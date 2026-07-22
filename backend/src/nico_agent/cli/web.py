"""Thin CLI coordinator for server-owned Web Provider configuration."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from nico_agent.cli.client import NicoApiClient
from nico_agent.cli.errors import CliError
from nico_agent.cli.output import Output
from nico_agent.cli.renderers import ProviderSetupRenderer
from nico_agent.cli.service_bridge import SecretAttempt, ServiceBridge

_TOOL_REFERENCE = re.compile(r"^env:NICO_TOOL_SECRET_[A-Z0-9_]{1,100}$")


@dataclass(slots=True)
class WebConfigureInput:
    provider: str | None = None
    endpoint_key: str | None = None
    credential_ref: str | None = None
    project_id: str | None = None
    agent_id: str | None = None
    expected_agent_revision: int | None = None
    starter_agent_name: str | None = None
    starter_agent_display_name: str | None = None
    safe_search: str = "moderate"
    cache_ttl_seconds: int = 900
    rate_limit_per_minute: int = 20
    allowed_domains: list[str] = field(default_factory=list)
    confirmed: bool = False


class _LeaseKeeper:
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
        self._thread = threading.Thread(target=self._run, name="nico-web-maintenance", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def check(self) -> None:
        if self._failure is not None:
            raise self._failure

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=46)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.bridge.renew(self.attempt)
            except CliError as exc:
                self._failure = exc
                self._stop.set()


class WebCoordinator:
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

    def configure(self, values: WebConfigureInput) -> dict[str, Any]:
        readiness = self._readiness()
        catalog = self._catalog()
        provider = self._provider(catalog, values.provider)
        endpoint_key = self._endpoint(provider, values.endpoint_key)
        attempt: SecretAttempt | None = None
        keeper: _LeaseKeeper | None = None
        published = False
        committed = False
        try:
            credential_ref = values.credential_ref
            if provider.get("requires_secret"):
                credential_ref, attempt = self._credential(provider, credential_ref)
                if attempt is not None:
                    keeper = _LeaseKeeper(
                        self.service_bridge,  # type: ignore[arg-type]
                        attempt,
                        interval_seconds=self.renew_interval_seconds,
                    )
                    keeper.start()
            elif credential_ref is not None:
                raise CliError(
                    "WEB_PROVIDER_CREDENTIAL_INVALID",
                    "the selected Web Provider does not accept a credential reference",
                    exit_code=2,
                )
            candidate = {
                "schema_version": 1,
                "provider": provider["key"],
                "endpoint_key": endpoint_key,
                "credential_ref": credential_ref,
                "policy": {
                    "safe_search": values.safe_search,
                    "cache_ttl_seconds": values.cache_ttl_seconds,
                    "rate_limit_per_minute": values.rate_limit_per_minute,
                    "allowed_domains": values.allowed_domains,
                },
                "catalog_revision": catalog["catalog_revision"],
            }
            with self.renderer.progress(f"Testing {provider['display_name']}…"):
                probe = self._probe(candidate)
            if keeper is not None:
                keeper.check()
            self._require_probe_success(probe)
            target = self._target(values, readiness)
            preview = self.client.preview_web_activation(
                probe_id=str(probe["id"]),
                candidate_hash=str(probe["candidate_hash"]),
                target=target,
            )
            self._preview(preview, action="Enable Web access")
            confirmed = values.confirmed
            if not confirmed and self.interactive:
                confirmed = self.confirm("Publish this Web configuration?", default=False)
            if not confirmed:
                raise CliError(
                    "WEB_ACTIVATION_CANCELLED",
                    "Web Provider activation was not confirmed",
                    exit_code=2,
                )
            if keeper is not None:
                keeper.check()
            with self.renderer.progress("Publishing Web access…"):
                activation = self.client.activate_web(
                    probe_id=str(probe["id"]),
                    candidate_hash=str(probe["candidate_hash"]),
                    target=target,
                    preview_hash=str(preview["preview_hash"]),
                    maintenance_attempt_id=attempt.attempt_id if attempt else None,
                )
            published = True
            if attempt is not None:
                if keeper is not None:
                    keeper.stop()
                with self.renderer.progress("Finalizing the Web credential…"):
                    self.service_bridge.commit(attempt)  # type: ignore[union-attr]
                committed = True
            return {
                "status": "ready",
                "provider": provider["key"],
                "activation": activation,
            }
        finally:
            if keeper is not None:
                keeper.stop()
            if attempt is not None and not committed and not published:
                try:
                    self.service_bridge.rollback(attempt)  # type: ignore[union-attr]
                except CliError:
                    if not self.output.json_mode:
                        self.output.err.print(
                            "[red]Credential rollback requires: "
                            "nico-service credential-secret recover[/red]"
                        )

    def status(self) -> dict[str, Any]:
        status = self.client.web_status()
        secret_available: bool | None = None
        credential_ref = status.get("credential_ref")
        if status.get("secret_required") and isinstance(credential_ref, str):
            if self.service_bridge is not None:
                try:
                    secret_available = self.service_bridge.credential_available(credential_ref)
                except CliError:
                    secret_available = None
            if secret_available is False:
                status["diagnosis"] = "secret_unavailable"
        status["secret_available"] = secret_available
        return status

    def test(self) -> dict[str, Any]:
        self._readiness()
        with self.renderer.progress("Testing the configured Web Provider…"):
            created = self.client.test_web_configuration(
                idempotency_key=f"cli-web-test-{uuid4().hex}"
            )
            probe = self._wait_probe(created)
        self._require_probe_success(probe)
        return {"status": "succeeded", "probe": probe}

    def disable(self, *, agent_id: str | None, confirmed: bool) -> dict[str, Any]:
        readiness = self._readiness()
        status = self.client.web_status()
        if status.get("enabled") is not True:
            raise CliError(
                "WEB_ALREADY_DISABLED",
                "Web access is not currently enabled",
                exit_code=2,
            )
        agents = status.get("agents") or []
        selected = self._selected_agent(agents, agent_id)
        target = {
            "agent_id": selected["id"],
            "expected_tenant_revision": readiness["tenant_revision"],
            "expected_agent_revision": selected["revision"],
        }
        preview = self.client.preview_web_disable(target=target)
        self._preview(preview, action="Disable Web access")
        approved = confirmed
        if not approved and self.interactive:
            approved = self.confirm("Publish a new version without Web access?", default=False)
        if not approved:
            raise CliError(
                "WEB_DISABLE_CANCELLED",
                "Web disable was not confirmed",
                exit_code=2,
            )
        with self.renderer.progress("Publishing Web-disabled Agent version…"):
            result = self.client.disable_web(
                target=target,
                preview_hash=str(preview["preview_hash"]),
            )
        return {"status": "disabled", "activation": result}

    def _readiness(self) -> dict[str, Any]:
        readiness = self.client.web_setup_readiness()
        if readiness.get("writes_enabled") is not True:
            raise CliError(
                "WEB_PROVIDER_WRITES_DISABLED",
                "Web Provider changes are disabled by deployment policy",
                exit_code=3,
            )
        return readiness

    def _catalog(self) -> dict[str, Any]:
        catalog = self.client.web_catalog()
        if catalog.get("schema_version") != 1:
            raise CliError(
                "WEB_CATALOG_UNSUPPORTED",
                "this CLI does not support the server Web Provider catalog schema",
                exit_code=3,
            )
        return catalog

    def _provider(self, catalog: dict[str, Any], requested: str | None) -> dict[str, Any]:
        providers = catalog.get("providers") or []
        key = requested
        if key is None:
            self._require_interactive("Provider")
            self.output.table(
                [
                    {
                        "number": index,
                        "provider": item.get("key"),
                        "name": item.get("display_name"),
                        "credential": "required" if item.get("requires_secret") else "none",
                    }
                    for index, item in enumerate(providers, start=1)
                ],
                title="Web Providers",
                columns=["number", "provider", "name", "credential"],
            )
            selected = self.prompt("Provider number or key").strip().lower()
            if selected.isdigit() and 1 <= int(selected) <= len(providers):
                key = str(providers[int(selected) - 1]["key"])
            else:
                key = selected
        provider = next((item for item in providers if item.get("key") == key), None)
        if provider is None:
            raise CliError(
                "WEB_PROVIDER_NOT_FOUND",
                f"Web Provider '{key}' is not available",
                exit_code=2,
            )
        return provider

    @staticmethod
    def _endpoint(provider: dict[str, Any], requested: str | None) -> str:
        endpoints = provider.get("endpoints") or []
        selected = next((item for item in endpoints if item.get("key") == requested), None)
        if requested is not None and selected is None:
            raise CliError(
                "WEB_PROVIDER_ENDPOINT_INVALID",
                "the selected Web Provider endpoint is unavailable",
                exit_code=2,
            )
        selected = selected or next(
            (item for item in endpoints if item.get("default")),
            endpoints[0] if endpoints else None,
        )
        if selected is None:
            raise CliError("WEB_PROVIDER_ENDPOINT_INVALID", "Web Provider has no endpoint")
        return str(selected["key"])

    def _credential(
        self,
        provider: dict[str, Any],
        requested: str | None,
    ) -> tuple[str, SecretAttempt | None]:
        credential_ref = requested
        attempt = None
        if credential_ref is None:
            self._require_interactive("credential reference or local API key")
            if self.service_bridge is None:
                raise CliError(
                    "LOCAL_SERVICE_UNAVAILABLE",
                    "local API-key entry requires a Nico service profile; use --credential-ref",
                    exit_code=3,
                )
            with self.renderer.progress("Checking local credential state…"):
                self.service_bridge.recover_credentials()
            secret = self.prompt(
                "Web Provider API key (input hidden; leave blank to use an existing reference)",
                hide_input=True,
            ).strip()
            if secret:
                env_name = self._new_env_name(str(provider["key"]))
                with self.renderer.progress("Securing the Web credential…"):
                    attempt = self.service_bridge.begin_credential(env_name, secret)
                secret = ""
                credential_ref = attempt.credential_ref
            else:
                credential_ref = self.prompt(
                    "Credential reference (env:NICO_TOOL_SECRET_*)"
                ).strip()
        if not isinstance(credential_ref, str) or _TOOL_REFERENCE.fullmatch(credential_ref) is None:
            raise CliError(
                "WEB_CREDENTIAL_REFERENCE_INVALID",
                "Web credentials must use env:NICO_TOOL_SECRET_*",
                exit_code=2,
            )
        return credential_ref, attempt

    def _target(
        self,
        values: WebConfigureInput,
        readiness: dict[str, Any],
    ) -> dict[str, Any]:
        project_id = values.project_id or self._project()
        if values.agent_id is not None:
            agent = self.client.get_agent(values.agent_id)
            revision = values.expected_agent_revision or agent.get("revision")
            if not isinstance(revision, int):
                raise CliError("WEB_AGENT_REVISION_REQUIRED", "Agent revision is required")
            return {
                "project_id": project_id,
                "expected_tenant_revision": readiness["tenant_revision"],
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
                        "expected_tenant_revision": readiness["tenant_revision"],
                        "agent_id": selection,
                        "expected_agent_revision": agent["revision"],
                    }
            starter_name = self.prompt("Starter Agent name", default="nico-web").strip()
            starter_display = self.prompt(
                "Starter Agent display name", default="Nico Web Researcher"
            ).strip()
        if not starter_display:
            raise CliError(
                "WEB_STARTER_AGENT_REQUIRED",
                "Starter Agent display name is required",
                exit_code=2,
            )
        return {
            "project_id": project_id,
            "expected_tenant_revision": readiness["tenant_revision"],
            "starter_agent_name": starter_name,
            "starter_agent_display_name": starter_display,
        }

    def _project(self) -> str:
        projects = [item for item in self.client.list_projects() if item.get("status") == "active"]
        if not projects:
            raise CliError("WEB_PROJECT_REQUIRED", "create an active Project first", exit_code=2)
        if len(projects) == 1:
            return str(projects[0]["id"])
        self._require_interactive("Project")
        self.output.table(projects, title="Projects", columns=["id", "name"])
        return self.prompt("Project ID").strip()

    def _probe(self, candidate: dict[str, Any]) -> dict[str, Any]:
        return self._wait_probe(
            self.client.create_web_probe(
                candidate=candidate,
                idempotency_key=f"cli-web-probe-{uuid4().hex}",
            )
        )

    def _wait_probe(self, created: dict[str, Any]) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout_seconds
        current = created
        while current.get("status") in {"pending", "running"}:
            if time.monotonic() >= deadline:
                raise CliError(
                    "WEB_PROBE_TIMEOUT",
                    "Web Provider verification timed out",
                    exit_code=4,
                )
            time.sleep(self.poll_interval)
            current = self.client.get_web_probe(str(current["id"]))
        return current

    @staticmethod
    def _require_probe_success(probe: dict[str, Any]) -> None:
        if probe.get("status") != "succeeded" or not probe.get("verified_at"):
            raise CliError(
                str(probe.get("error_code") or "WEB_PROVIDER_VERIFICATION_FAILED"),
                str(probe.get("error_detail") or "Web Provider verification failed"),
                exit_code=4,
            )

    def _selected_agent(
        self,
        agents: list[dict[str, Any]],
        requested: str | None,
    ) -> dict[str, Any]:
        if requested is not None:
            selected = next((item for item in agents if str(item.get("id")) == requested), None)
            if selected is None:
                raise CliError(
                    "WEB_AGENT_NOT_AUTHORIZED",
                    "the selected Agent does not have Web access",
                    exit_code=2,
                )
            return selected
        if len(agents) == 1:
            return agents[0]
        self._require_interactive("Agent")
        self.output.table(agents, title="Web-enabled Agents", columns=["id", "name", "revision"])
        selected_id = self.prompt("Agent ID").strip()
        return self._selected_agent(agents, selected_id)

    def _preview(self, preview: dict[str, Any], *, action: str) -> None:
        if self.output.json_mode:
            return
        projection = preview.get("projection") or {}
        agent = projection.get("agent") or {}
        version = projection.get("agent_version") or {}
        self.output.table(
            [
                {
                    "action": action,
                    "agent": agent.get("name") or agent.get("id"),
                    "new_version": version.get("version"),
                    "changes": len(preview.get("changed_fields") or []),
                }
            ],
            title="Web Configuration Preview",
            columns=["action", "agent", "new_version", "changes"],
        )

    def _require_interactive(self, field_name: str) -> None:
        if not self.interactive:
            raise CliError(
                "WEB_INPUT_REQUIRED",
                f"non-interactive Web setup requires an explicit {field_name}",
                exit_code=2,
            )

    @staticmethod
    def _new_env_name(provider: str) -> str:
        label = re.sub(r"[^A-Z0-9]", "_", provider.upper())
        return f"NICO_TOOL_SECRET_WEB_SEARCH_{label}_{uuid4().hex[:12].upper()}"
