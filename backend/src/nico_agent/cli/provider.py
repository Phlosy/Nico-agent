"""Shared Provider onboarding coordinator for interactive and JSON CLI flows."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from nico_agent.cli.client import NicoApiClient
from nico_agent.cli.errors import CliError
from nico_agent.cli.output import Output
from nico_agent.cli.service_bridge import SecretAttempt, ServiceBridge

_REFERENCE = re.compile(
    r"^(?:env:NICO_MODEL_SECRET_[A-Z0-9_]{1,100}|secret:[A-Za-z0-9._:/-]{1,240})$"
)


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
    confirmed: bool = False


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
        poll_interval: float = 1.0,
        timeout_seconds: float = 90.0,
    ) -> None:
        self.client = client
        self.output = output
        self.service_bridge = service_bridge
        self.interactive = interactive
        self.prompt = prompt
        self.confirm = confirm
        self.poll_interval = poll_interval
        self.timeout_seconds = timeout_seconds

    def onboard(self, values: ProviderInput) -> dict[str, Any]:
        catalog = self._catalog()
        provider = self._provider(catalog, values.provider_key)
        base_url = self._location(provider, values.location_key)
        attempt: SecretAttempt | None = None
        committed = False
        try:
            credential_ref = values.credential_ref
            if credential_ref is None:
                self._require_interactive("credential reference or local API key")
                selection = self.prompt(
                    "Credential reference, or 'key' to enter a local API key",
                    default="key",
                ).strip()
                if selection == "key":
                    if self.service_bridge is None:
                        raise CliError(
                            "LOCAL_SERVICE_UNAVAILABLE",
                            "local API-key entry requires an installed Nico service profile",
                            exit_code=3,
                        )
                    self.service_bridge.recover()
                    secret = self.prompt("Provider API key", hide_input=True).strip()
                    if not secret:
                        raise CliError(
                            "PROVIDER_CREDENTIAL_REQUIRED",
                            "Provider API key cannot be blank",
                            exit_code=2,
                        )
                    env_name = self._new_env_name(provider["key"])
                    attempt = self.service_bridge.begin(env_name, secret)
                    secret = ""
                    credential_ref = attempt.credential_ref
                else:
                    credential_ref = selection
            self._validate_reference(credential_ref)

            model = values.model or self._discover_or_recommend(
                provider,
                catalog_revision=catalog["catalog_revision"],
                base_url=base_url,
                credential_ref=credential_ref,
                provider_options=values.provider_options,
                attempt=attempt,
            )
            candidate = self._candidate(
                provider,
                catalog_revision=catalog["catalog_revision"],
                base_url=base_url,
                credential_ref=credential_ref,
                model=model,
                provider_options=values.provider_options,
            )
            verified = self._probe("verify_completion", candidate, attempt=attempt)
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
            if attempt is not None:
                self.service_bridge.renew(attempt)
            activated = self.client.activate_provider(
                probe_id=str(verified["id"]),
                candidate_hash=str(verified["candidate_hash"]),
                target=target,
                preview_hash=str(preview["preview_hash"]),
                maintenance_attempt_id=attempt.attempt_id if attempt else None,
            )
            if attempt is not None:
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
            if attempt is not None and not committed:
                try:
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
        return self._probe("verify_completion", candidate, attempt=None)

    def _catalog(self) -> dict[str, Any]:
        catalog = self.client.provider_catalog()
        if catalog.get("schema_version") != 1:
            raise CliError(
                "PROVIDER_CATALOG_UNSUPPORTED",
                "this CLI does not support the server Provider catalog schema",
                exit_code=3,
            )
        return catalog

    def _provider(self, catalog: dict[str, Any], requested: str | None) -> dict[str, Any]:
        providers = catalog.get("providers") or []
        key = requested
        if key is None:
            self._require_interactive("Provider")
            if not self.output.json_mode:
                self.output.table(
                    [
                        {"number": index, "key": item["key"], "name": item["display_name"]}
                        for index, item in enumerate(providers, start=1)
                    ],
                    title="Provider Presets",
                    columns=["number", "key", "name"],
                )
            selected = self.prompt("Provider number or key").strip()
            if selected.isdigit() and 1 <= int(selected) <= len(providers):
                key = str(providers[int(selected) - 1]["key"])
            else:
                key = selected
        provider = next((item for item in providers if item.get("key") == key), None)
        if provider is None:
            raise CliError(
                "PROVIDER_NOT_FOUND",
                f"Provider preset '{key}' is not available",
                exit_code=2,
            )
        return provider

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
        attempt: SecretAttempt | None,
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
            discovered = self._probe("discover_models", candidate, attempt=attempt)
            if discovered.get("status") == "succeeded":
                models = [
                    str(item["id"])
                    for item in discovered.get("result", {}).get("models", [])[:20]
                    if isinstance(item, dict) and item.get("id")
                ]
        recommendations = [str(value) for value in provider.get("recommended_models") or []]
        ordered = list(dict.fromkeys([*recommendations, *models]))
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
        if not self.output.json_mode:
            self.output.table(
                [
                    {"number": index, "model": model, "recommended": model in recommendations}
                    for index, model in enumerate(ordered[:20], start=1)
                ],
                title="Models",
                columns=["number", "model", "recommended"],
            )
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
        *,
        attempt: SecretAttempt | None,
    ) -> dict[str, Any]:
        created = self.client.create_provider_probe(
            kind=kind,
            candidate=candidate,
            idempotency_key=f"cli-{kind}-{uuid4().hex}",
        )
        deadline = time.monotonic() + self.timeout_seconds
        last_renewal = time.monotonic()
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
            if attempt is not None and time.monotonic() - last_renewal >= 30:
                self.service_bridge.renew(attempt)
                last_renewal = time.monotonic()
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
