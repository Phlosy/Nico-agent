"""Shared Provider onboarding coordinator for interactive and JSON CLI flows."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from nico_agent.cli.client import NicoApiClient
from nico_agent.cli.errors import CliError
from nico_agent.cli.output import Output
from nico_agent.cli.renderers import ProviderSetupRenderer
from nico_agent.cli.service_bridge import SecretAttempt, ServiceBridge

_REFERENCE = re.compile(
    r"^(?:env:NICO_MODEL_SECRET_[A-Z0-9_]{1,100}|secret:[A-Za-z0-9._:/-]{1,240})$"
)
_LEASE_KEEPER_STOP_TIMEOUT_SECONDS = 46


@dataclass(slots=True)
class ProviderInput:
    provider_key: str | None = None
    credential_ref: str | None = None
    model: str | None = None
    project_id: str | None = None
    agent_id: str | None = None
    expected_agent_revision: int | None = None
    starter_agent_name: str | None = None
    starter_agent_display_name: str | None = None
    location_key: str | None = None
    provider_options: dict[str, Any] = field(default_factory=dict)
    custom_provider_key: str | None = None
    custom_provider_name: str | None = None
    custom_protocol: str | None = None
    custom_base_url: str | None = None
    confirmed: bool = False


class _MaintenanceLeaseKeeper:
    def __init__(
        self,
        bridge: ServiceBridge,
        attempt: SecretAttempt,
        *,
        interval_seconds: float,
    ) -> None:
        self.bridge = bridge
        self.attempt = attempt
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._failure: CliError | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="nico-provider-maintenance",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def check(self) -> None:
        if self._failure is not None:
            raise self._failure

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=_LEASE_KEEPER_STOP_TIMEOUT_SECONDS)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.bridge.renew(self.attempt)
            except CliError as exc:
                self._failure = exc
                self._stop.set()


class ProviderOnboardingCoordinator:
    def __init__(
        self,
        client: NicoApiClient,
        output: Output,
        *,
        service_bridge: ServiceBridge | None,
        interactive: bool,
        prompt: Callable[..., str],
        confirm: Callable[..., bool],
        poll_interval: float = 0.25,
        timeout_seconds: float = 90.0,
        renew_interval_seconds: float = 30.0,
    ) -> None:
        if renew_interval_seconds <= 0:
            raise ValueError("renew interval must be positive")
        self.client = client
        self.output = output
        self.service_bridge = service_bridge
        self.interactive = interactive
        self.prompt = prompt
        self.confirm = confirm
        self.poll_interval = poll_interval
        self.timeout_seconds = timeout_seconds
        self.renew_interval_seconds = renew_interval_seconds
        self.renderer = ProviderSetupRenderer(output)

    def onboard(self, values: ProviderInput) -> dict[str, Any]:
        catalog = self._catalog()
        provider = self._provider(catalog, values)
        base_url = self._location(provider, values.location_key)
        provider_options = dict(values.provider_options)
        if provider.get("custom"):
            provider_options["nico_custom_display_name"] = provider["display_name"]
        attempt: SecretAttempt | None = None
        keeper: _MaintenanceLeaseKeeper | None = None
        published = False
        committed = False
        try:
            credential_ref = values.credential_ref
            if credential_ref is None:
                self._require_interactive("credential reference or local API key")
                if self.service_bridge is None:
                    raise CliError(
                        "LOCAL_SERVICE_UNAVAILABLE",
                        "local API-key entry requires an installed Nico service profile; "
                        "use --credential-ref for an existing secret",
                        exit_code=3,
                    )
                with self.renderer.progress("Checking local credential state…"):
                    self.service_bridge.recover()
                secret = self.prompt(
                    "Provider API key (input hidden; leave blank to use an existing reference)",
                    hide_input=True,
                ).strip()
                if not secret:
                    credential_ref = self.prompt(
                        "Credential reference (env:NICO_MODEL_SECRET_* or secret:*)"
                    ).strip()
                else:
                    env_name = self._new_env_name(provider["key"])
                    with self.renderer.progress(
                        "Securing API key and refreshing the model worker…"
                    ):
                        attempt = self.service_bridge.begin(env_name, secret)
                    keeper = _MaintenanceLeaseKeeper(
                        self.service_bridge,
                        attempt,
                        interval_seconds=self.renew_interval_seconds,
                    )
                    keeper.start()
                    secret = ""
                    credential_ref = attempt.credential_ref
            self._validate_reference(credential_ref)
            if keeper is not None:
                keeper.check()

            model = values.model or self._discover_or_recommend(
                provider,
                catalog_revision=catalog["catalog_revision"],
                base_url=base_url,
                credential_ref=credential_ref,
                provider_options=provider_options,
            )
            candidate = self._candidate(
                provider,
                catalog_revision=catalog["catalog_revision"],
                base_url=base_url,
                credential_ref=credential_ref,
                model=model,
                provider_options=provider_options,
            )
            with self.renderer.progress(f"Verifying model {model}…"):
                verified = self._probe("verify_completion", candidate)
            if keeper is not None:
                keeper.check()
            if verified.get("status") != "succeeded" or not verified.get("verified_at"):
                raise CliError(
                    str(verified.get("error_code") or "PROVIDER_VERIFICATION_FAILED"),
                    str(verified.get("error_detail") or "Provider verification failed"),
                    exit_code=4,
                )
            project_id = self._project(values.project_id)
            target = self._target(values, project_id)
            preview = self.client.preview_provider_activation(
                probe_id=str(verified["id"]),
                candidate_hash=str(verified["candidate_hash"]),
                target=target,
            )
            if not self.output.json_mode:
                self.output.emit(preview, title="Provider Activation Preview")
            confirmed = values.confirmed
            if not confirmed and self.interactive:
                confirmed = self.confirm("Publish this Provider route?", default=False)
            if not confirmed:
                raise CliError(
                    "PROVIDER_ACTIVATION_CANCELLED",
                    "Provider activation was not confirmed",
                    exit_code=2,
                )
            if keeper is not None:
                keeper.check()
            with self.renderer.progress("Publishing the Provider route…"):
                activated = self.client.activate_provider(
                    probe_id=str(verified["id"]),
                    candidate_hash=str(verified["candidate_hash"]),
                    target=target,
                    preview_hash=str(preview["preview_hash"]),
                    maintenance_attempt_id=attempt.attempt_id if attempt else None,
                )
            published = True
            if attempt is not None:
                if keeper is not None:
                    keeper.stop()
                with self.renderer.progress("Finalizing the Provider credential…"):
                    self.service_bridge.commit(attempt)
                committed = True
            return {
                "status": "ready",
                "provider": provider["key"],
                "model": model,
                "activation": activated,
                "chat_command": (
                    f"nico chat --project {project_id} --agent {activated['agent_id']}"
                ),
            }
        finally:
            if keeper is not None:
                keeper.stop()
            if attempt is not None and not committed and not published:
                try:
                    with self.renderer.progress("Removing the uncommitted API key…"):
                        self.service_bridge.rollback(attempt)
                except CliError:
                    if not self.output.json_mode:
                        self.output.err.print(
                            "[red]Provider secret rollback requires: nico-service "
                            "provider-secret recover[/red]"
                        )

    def test_existing(self, provider_key: str) -> dict[str, Any]:
        connections = self.client.list_provider_connections()
        connection = next(
            (item for item in connections if item.get("provider_key") == provider_key),
            None,
        )
        if connection is None:
            raise CliError(
                "PROVIDER_CONNECTION_NOT_FOUND",
                f"Provider connection '{provider_key}' was not found",
                exit_code=2,
            )
        models = connection.get("allowed_models") or []
        if not models:
            raise CliError(
                "PROVIDER_MODEL_REQUIRED",
                "the Provider connection has no configured model",
                exit_code=2,
            )
        candidate = {
            "schema_version": 1,
            "provider_key": connection["provider_key"],
            "protocol": connection["protocol"],
            "base_url": connection["base_url"],
            "credential_ref": connection["credential_ref"],
            "model": models[0],
            "provider_options": connection.get("provider_options") or {},
            "catalog_revision": connection["catalog_revision"],
        }
        return self._probe("verify_completion", candidate)

    def _catalog(self) -> dict[str, Any]:
        catalog = self.client.provider_catalog()
        if catalog.get("schema_version") != 1:
            raise CliError(
                "PROVIDER_CATALOG_UNSUPPORTED",
                "this CLI does not support the server Provider catalog schema",
                exit_code=3,
            )
        return catalog

    def _provider(self, catalog: dict[str, Any], values: ProviderInput) -> dict[str, Any]:
        providers = catalog.get("providers") or []
        key = values.provider_key
        if key is None:
            self._require_interactive("Provider")
            self.renderer.welcome()
            self.renderer.providers(providers)
            selected = self.prompt("Provider number or key").strip()
            if selected.isdigit() and 1 <= int(selected) <= len(providers):
                key = str(providers[int(selected) - 1]["key"])
            elif selected.isdigit() and int(selected) == len(providers) + 1:
                key = "other"
            else:
                key = selected.lower()
        if key in {"other", "custom"} or str(key).startswith("custom-"):
            return self._custom_provider(values)
        provider = next((item for item in providers if item.get("key") == key), None)
        if provider is None:
            raise CliError(
                "PROVIDER_NOT_FOUND",
                f"Provider preset '{key}' is not available",
                exit_code=2,
            )
        return provider

    def _custom_provider(self, values: ProviderInput) -> dict[str, Any]:
        name = values.custom_provider_name
        protocol = values.custom_protocol
        base_url = values.custom_base_url
        if name is None or protocol is None or base_url is None:
            self._require_interactive("custom Provider name, protocol, and base URL")
        name = name or self.prompt("Provider display name", default="Local Model").strip()
        if protocol is None:
            self.renderer.protocols()
            selected = self.prompt("Protocol number or key", default="1").strip()
            protocol = {
                "1": "openai_compatible",
                "2": "anthropic_messages",
                "3": "google_gemini",
            }.get(selected, selected)
        if protocol not in {"openai_compatible", "anthropic_messages", "google_gemini"}:
            raise CliError(
                "PROVIDER_PROTOCOL_ERROR",
                "protocol must be openai_compatible, anthropic_messages, or google_gemini",
                exit_code=2,
            )
        base_url = (
            base_url
            or self.prompt(
                "Base URL",
                default="http://localhost:11434/v1",
            ).strip()
        )
        base_url = self._container_reachable_url(base_url)
        key = values.custom_provider_key
        if key is None and values.provider_key and values.provider_key.startswith("custom-"):
            key = values.provider_key
        key = key or f"custom-{self._slug(name)}"
        if (
            re.fullmatch(
                r"custom-[a-z0-9](?:[a-z0-9_-]{0,111}[a-z0-9])?",
                key,
            )
            is None
        ):
            raise CliError(
                "PROVIDER_KEY_INVALID",
                "custom Provider key must start with 'custom-' and contain lowercase letters, "
                "numbers, '-' or '_'",
                exit_code=2,
            )
        discovery = {
            "openai_compatible": "openai_models",
            "anthropic_messages": "anthropic_models",
            "google_gemini": "gemini_models",
        }[protocol]
        return {
            "key": key,
            "display_name": name,
            "protocol": protocol,
            "locations": [{"key": "custom", "base_url": base_url, "default": True}],
            "discovery": discovery,
            "recommended_models": [],
            "capabilities": {
                "streaming": True,
                "native_tool_calling": True,
                "json_object": True,
            },
            "custom": True,
        }

    def _location(self, provider: dict[str, Any], requested: str | None) -> str:
        locations = provider.get("locations") or []
        selected = next(
            (item for item in locations if item.get("key") == requested),
            None,
        )
        if requested is not None and selected is None:
            raise CliError(
                "PROVIDER_LOCATION_INVALID",
                f"location '{requested}' is not available for {provider['key']}",
                exit_code=2,
            )
        selected = selected or next(
            (item for item in locations if item.get("default")),
            locations[0] if locations else None,
        )
        if selected is None:
            raise CliError("PROVIDER_LOCATION_INVALID", "Provider has no service location")
        return str(selected["base_url"])

    def _discover_or_recommend(
        self,
        provider: dict[str, Any],
        *,
        catalog_revision: str,
        base_url: str,
        credential_ref: str,
        provider_options: dict[str, Any],
    ) -> str:
        models: list[str] = []
        if provider.get("discovery") != "curated":
            candidate = self._candidate(
                provider,
                catalog_revision=catalog_revision,
                base_url=base_url,
                credential_ref=credential_ref,
                model=None,
                provider_options=provider_options,
            )
            with self.renderer.progress(f"Discovering models from {provider['display_name']}…"):
                discovered = self._probe("discover_models", candidate)
            if discovered.get("status") == "succeeded":
                models = [
                    str(item["id"])
                    for item in discovered.get("result", {}).get("models", [])[:20]
                    if isinstance(item, dict) and item.get("id")
                ]
        recommendations = [str(value) for value in provider.get("recommended_models") or []]
        ordered = list(dict.fromkeys([*recommendations, *models]))
        if not ordered and self.interactive:
            selected = self.prompt("Exact model ID").strip()
            if selected:
                return selected
        if not ordered:
            raise CliError(
                "PROVIDER_MODEL_REQUIRED",
                "model discovery returned no choices; provide --model explicitly",
                exit_code=2,
            )
        if not self.interactive:
            raise CliError(
                "PROVIDER_MODEL_REQUIRED",
                "non-interactive Provider setup requires --model",
                exit_code=2,
            )
        self.renderer.models(ordered[:20], set(recommendations))
        selected = self.prompt(
            "Model number or exact model ID",
            default=ordered[0],
        ).strip()
        if selected.isdigit() and 1 <= int(selected) <= len(ordered[:20]):
            return ordered[int(selected) - 1]
        return selected

    def _project(self, requested: str | None) -> str:
        projects = [item for item in self.client.list_projects() if item.get("status") == "active"]
        if not projects:
            raise CliError(
                "PROVIDER_PROJECT_REQUIRED",
                "create a Project with 'nico project create' or the API before Provider setup",
                exit_code=2,
            )
        if requested is not None:
            if not any(str(item.get("id")) == requested for item in projects):
                raise CliError("PROVIDER_PROJECT_NOT_FOUND", "selected Project is not active")
            return requested
        self._require_interactive("Project")
        if len(projects) == 1:
            return str(projects[0]["id"])
        self.output.table(projects, title="Projects", columns=["id", "name"])
        return self.prompt("Project ID").strip()

    def _target(self, values: ProviderInput, project_id: str) -> dict[str, Any]:
        if values.agent_id is not None:
            agent = self.client.get_agent(values.agent_id)
            revision = values.expected_agent_revision or agent.get("revision")
            if not isinstance(revision, int):
                raise CliError("PROVIDER_AGENT_REVISION_REQUIRED", "Agent revision is required")
            return {
                "project_id": project_id,
                "agent_id": values.agent_id,
                "expected_agent_revision": revision,
            }
        starter_name = values.starter_agent_name
        starter_display = values.starter_agent_display_name
        if starter_name is None:
            self._require_interactive("Agent selection")
            agents = self.client.list_agents()
            if agents:
                self.output.table(
                    agents,
                    title="Agents",
                    columns=["id", "name", "display_name", "status", "revision"],
                )
                selection = self.prompt("Agent ID, or 'starter'", default="starter").strip()
                if selection != "starter":
                    agent = self.client.get_agent(selection)
                    return {
                        "project_id": project_id,
                        "agent_id": selection,
                        "expected_agent_revision": agent["revision"],
                    }
            starter_name = self.prompt("Starter Agent name", default="nico-assistant").strip()
            starter_display = self.prompt(
                "Starter Agent display name",
                default="Nico Assistant",
            ).strip()
        if not starter_display:
            raise CliError(
                "PROVIDER_STARTER_AGENT_REQUIRED",
                "Starter Agent display name is required",
                exit_code=2,
            )
        return {
            "project_id": project_id,
            "starter_agent_name": starter_name,
            "starter_agent_display_name": starter_display,
        }

    def _probe(
        self,
        kind: str,
        candidate: dict[str, Any],
    ) -> dict[str, Any]:
        created = self.client.create_provider_probe(
            kind=kind,
            candidate=candidate,
            idempotency_key=f"cli-{kind}-{uuid4().hex}",
        )
        deadline = time.monotonic() + self.timeout_seconds
        current = created
        while current.get("status") in {"pending", "running"}:
            if time.monotonic() >= deadline:
                try:
                    self.client.cancel_provider_probe(str(current["id"]))
                except CliError:
                    pass
                raise CliError(
                    "PROVIDER_PROBE_TIMEOUT",
                    "Provider verification timed out",
                    exit_code=4,
                )
            time.sleep(self.poll_interval)
            current = self.client.get_provider_probe(str(current["id"]))
        return current

    @staticmethod
    def _candidate(
        provider: dict[str, Any],
        *,
        catalog_revision: str,
        base_url: str,
        credential_ref: str,
        model: str | None,
        provider_options: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "provider_key": provider["key"],
            "protocol": provider["protocol"],
            "base_url": base_url,
            "credential_ref": credential_ref,
            "model": model,
            "provider_options": provider_options,
            "catalog_revision": catalog_revision,
        }

    def _require_interactive(self, field_name: str) -> None:
        if not self.interactive:
            raise CliError(
                "PROVIDER_INPUT_REQUIRED",
                f"non-interactive Provider setup requires an explicit {field_name}",
                exit_code=2,
            )

    @staticmethod
    def _validate_reference(value: str) -> None:
        if _REFERENCE.fullmatch(value) is None:
            raise CliError(
                "PROVIDER_CREDENTIAL_REFERENCE_INVALID",
                "credential must be an env:NICO_MODEL_SECRET_* or secret:* reference",
                exit_code=2,
            )

    @staticmethod
    def _new_env_name(provider_key: str) -> str:
        label = re.sub(r"[^A-Z0-9]", "_", provider_key.upper())
        return f"NICO_MODEL_SECRET_{label}_{uuid4().hex[:12].upper()}"

    @staticmethod
    def _slug(value: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
        return slug[:80] or "provider"

    @staticmethod
    def _container_reachable_url(value: str) -> str:
        parsed = urlsplit(value.strip())
        try:
            port = parsed.port
        except ValueError as exc:
            raise CliError(
                "PROVIDER_LOCATION_INVALID",
                "Base URL contains an invalid port",
                exit_code=2,
            ) from exc
        if parsed.username or parsed.password:
            raise CliError(
                "PROVIDER_LOCATION_INVALID",
                "Base URL must not contain credentials",
                exit_code=2,
            )
        if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            return value.strip()
        port_suffix = f":{port}" if port else ""
        return urlunsplit(
            (
                parsed.scheme,
                f"host.docker.internal{port_suffix}",
                parsed.path,
                parsed.query,
                parsed.fragment,
            )
        )
