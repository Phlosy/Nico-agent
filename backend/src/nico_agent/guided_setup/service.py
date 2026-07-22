"""Tenant-scoped guided setup readiness and resumable intent ledger."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select

from nico_agent.config import Settings
from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import ResourceNotFound
from nico_agent.domain.models import (
    Agent,
    AgentVersion,
    AuditRecord,
    ModelEndpoint,
    ProviderProbe,
    Run,
    RuntimeSession,
    Tenant,
    ToolCall,
)
from nico_agent.domain.states import require_revision
from nico_agent.guided_setup.contracts import (
    SetupAreaRead,
    SetupIntentPatch,
    SetupIntentRead,
    SetupProofCreate,
    SetupProofRead,
    SetupReadinessRead,
    SetupTargetRead,
)
from nico_agent.net.safe_http import SafeHttpError, canonicalize_http_url
from nico_agent.runtime.native.context import output_has_observed_web_citation

_LEDGER_KEY = "guided_setup"
_LEDGER_SCHEMA_VERSION = 1


def parse_guided_setup_ledger(settings: dict[str, Any]) -> dict[str, Any]:
    """Return a bounded, normalized ledger without trusting stored JSON shape."""

    raw = settings.get(_LEDGER_KEY)
    if not isinstance(raw, dict) or raw.get("schema_version") != _LEDGER_SCHEMA_VERSION:
        return {"schema_version": _LEDGER_SCHEMA_VERSION}
    ledger: dict[str, Any] = {"schema_version": _LEDGER_SCHEMA_VERSION}
    if raw.get("web_intent") in {"enabled", "skipped"}:
        ledger["web_intent"] = raw["web_intent"]
    for key in ("selected_agent_id", "capability_agent_version_id"):
        try:
            ledger[key] = str(UUID(str(raw[key])))
        except (KeyError, TypeError, ValueError):
            pass
    profile = raw.get("selected_profile")
    if isinstance(profile, str) and 0 < len(profile) <= 80:
        ledger["selected_profile"] = profile
    proof = raw.get("proof")
    if isinstance(proof, dict):
        normalized = _normalized_proof(proof)
        if normalized is not None:
            ledger["proof"] = normalized
    return ledger


def merge_guided_setup_ledger(
    settings: dict[str, Any],
    **changes: object,
) -> dict[str, Any]:
    """Merge guided state while preserving every unrelated Tenant setting."""

    merged = deepcopy(settings)
    ledger = parse_guided_setup_ledger(merged)
    for key, value in changes.items():
        if value is None:
            ledger.pop(key, None)
        else:
            ledger[key] = str(value) if isinstance(value, UUID) else value
    ledger["schema_version"] = _LEDGER_SCHEMA_VERSION
    merged[_LEDGER_KEY] = ledger
    return merged


class GuidedSetupService:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings

    async def readiness(self, context: TenantContext) -> SetupReadinessRead:
        async with self.database.tenant_transaction(context) as session:
            tenant = await session.scalar(select(Tenant).where(Tenant.id == context.tenant_id))
            if tenant is None:
                raise ResourceNotFound("tenant", str(context.tenant_id))
            settings = tenant.settings if isinstance(tenant.settings, dict) else {}
            ledger = parse_guided_setup_ledger(settings)

            route_rows = (
                await session.execute(
                    select(Agent, AgentVersion, ModelEndpoint)
                    .join(
                        AgentVersion,
                        (AgentVersion.tenant_id == Agent.tenant_id)
                        & (AgentVersion.agent_id == Agent.id)
                        & (AgentVersion.id == Agent.current_version_id),
                    )
                    .join(
                        ModelEndpoint,
                        (ModelEndpoint.tenant_id == AgentVersion.tenant_id)
                        & (ModelEndpoint.id == AgentVersion.model_endpoint_id),
                    )
                    .where(
                        Agent.tenant_id == context.tenant_id,
                        Agent.status == "ready",
                        AgentVersion.runtime_provider == "nico_native",
                        AgentVersion.status == "published",
                        ModelEndpoint.enabled.is_(True),
                        ModelEndpoint.verified_at.is_not(None),
                    )
                    .order_by(Agent.created_at, Agent.id)
                )
            ).all()
            selected_id = _uuid(ledger.get("selected_agent_id"))
            selected = next(
                (row for row in route_rows if row[0].id == selected_id),
                route_rows[0] if route_rows else None,
            )

            latest_model_probe = await session.scalar(
                select(ProviderProbe)
                .where(
                    ProviderProbe.tenant_id == context.tenant_id,
                    ProviderProbe.kind == "verify_completion",
                )
                .order_by(ProviderProbe.created_at.desc())
                .limit(1)
            )
            model_area = self._model_area(bool(route_rows), latest_model_probe)

            configured = _mapping(settings.get("web_provider"))
            web_candidate_hash = _hash(configured.get("candidate_hash"))
            latest_web_probe = None
            if web_candidate_hash is not None:
                latest_web_probe = await session.scalar(
                    select(ProviderProbe)
                    .where(
                        ProviderProbe.tenant_id == context.tenant_id,
                        ProviderProbe.kind == "verify_web",
                        ProviderProbe.candidate_hash == web_candidate_hash,
                    )
                    .order_by(ProviderProbe.created_at.desc())
                    .limit(1)
                )
            web_area = self._web_area(ledger, configured, latest_web_probe)

            target = None
            if selected is not None:
                agent, version, _endpoint = selected
                target = SetupTargetRead(
                    agent_id=agent.id,
                    agent_name=agent.name,
                    agent_revision=agent.revision,
                    agent_version_id=version.id,
                    agent_version=version.version,
                    model_name=version.model_name,
                )
            capability_version_id = _uuid(ledger.get("capability_agent_version_id"))
            selected_profile = ledger.get("selected_profile")
            web_authorized = bool(
                selected is not None and _version_web_authorized(selected[1], web_candidate_hash)
            )
            web_profile_current = bool(
                selected_profile in {"web_research", "developer"} and web_area.state == "ready"
            )
            capabilities_ready = bool(
                target is not None
                and capability_version_id == target.agent_version_id
                and selected_profile
                and (not web_profile_current or web_authorized)
            )
            capability_area = SetupAreaRead(
                key="capabilities",
                state="ready" if capabilities_ready else "incomplete",
                summary=(
                    f"Agent {target.agent_name} uses {ledger['selected_profile']} capabilities"
                    if capabilities_ready and target is not None
                    else "Agent capabilities have not been reviewed and published"
                ),
                next_action=None if capabilities_ready else "choose Agent capabilities",
                details={
                    "profile": str(selected_profile) if selected_profile else "",
                    "web_authorized": web_authorized,
                },
            )

            proof = _mapping(ledger.get("proof"))
            proof_area, verified_at = await self._proof_area(
                session,
                context.tenant_id,
                proof,
                target.agent_version_id if target is not None else None,
                web_candidate_hash,
                web_ready=web_area.state == "ready",
                web_authorized=web_authorized,
            )
            areas = (model_area, web_area, capability_area, proof_area)
            full = all(area.state == "ready" for area in areas)
            partial = (
                model_area.state == "ready"
                and capability_area.state == "ready"
                and web_area.state == "skipped"
            )
            provider_value = configured.get("provider")
            web_provider = provider_value if provider_value in {"brave", "searxng"} else None
            return SetupReadinessRead(
                tenant_revision=tenant.revision,
                overall="full" if full else ("partial" if partial else "incomplete"),
                areas=areas,
                target=target,
                selected_profile=(
                    str(ledger["selected_profile"]) if ledger.get("selected_profile") else None
                ),
                web_provider=web_provider,
                verified_at=verified_at,
            )

    async def update_intent(
        self,
        context: TenantContext,
        command: SetupIntentPatch,
    ) -> SetupIntentRead:
        async with self.database.tenant_transaction(context) as session:
            tenant = await session.scalar(
                select(Tenant).where(Tenant.id == context.tenant_id).with_for_update()
            )
            if tenant is None:
                raise ResourceNotFound("tenant", str(context.tenant_id))
            changes = {
                field: getattr(command, field)
                for field in (
                    "web_intent",
                    "selected_agent_id",
                    "selected_profile",
                    "capability_agent_version_id",
                )
                if field in command.model_fields_set
            }
            current = tenant.settings if isinstance(tenant.settings, dict) else {}
            ledger = parse_guided_setup_ledger(current)
            if _ledger_matches(ledger, changes):
                return _intent_read(tenant.revision, ledger)
            require_revision(
                "tenant",
                expected=command.expected_tenant_revision,
                actual=tenant.revision,
            )
            if command.selected_agent_id is not None:
                agent = await session.scalar(
                    select(Agent).where(
                        Agent.tenant_id == context.tenant_id,
                        Agent.id == command.selected_agent_id,
                    )
                )
                if agent is None:
                    raise ResourceNotFound("agent", str(command.selected_agent_id))
            tenant.settings = merge_guided_setup_ledger(current, **changes)
            tenant.revision += 1
            session.add(
                AuditRecord(
                    tenant_id=context.tenant_id,
                    action="guided_setup.intent.update",
                    resource_type="tenant",
                    resource_id=tenant.id,
                    actor_id=context.actor_id,
                    details={"changed_fields": sorted(changes)},
                    correlation_id=context.correlation_id,
                )
            )
            await session.flush()
            ledger = parse_guided_setup_ledger(tenant.settings)
            return _intent_read(tenant.revision, ledger)

    async def validate_proof(
        self,
        context: TenantContext,
        command: SetupProofCreate,
    ) -> SetupProofRead:
        async with self.database.tenant_transaction(context) as session:
            tenant = await session.scalar(
                select(Tenant).where(Tenant.id == context.tenant_id).with_for_update()
            )
            if tenant is None:
                raise ResourceNotFound("tenant", str(context.tenant_id))
            require_revision(
                "tenant",
                expected=command.expected_tenant_revision,
                actual=tenant.revision,
            )
            settings = tenant.settings if isinstance(tenant.settings, dict) else {}
            ledger = parse_guided_setup_ledger(settings)
            agent_id = _uuid(ledger.get("selected_agent_id"))
            version_id = _uuid(ledger.get("capability_agent_version_id"))
            configured = _mapping(settings.get("web_provider"))
            candidate_hash = _hash(configured.get("candidate_hash"))
            run = await session.scalar(
                select(Run).where(
                    Run.tenant_id == context.tenant_id,
                    Run.id == command.run_id,
                )
            )
            failure = self._proof_failure(
                run,
                expected_agent_id=agent_id,
                expected_version_id=version_id,
                candidate_hash=candidate_hash,
            )
            calls: tuple[ToolCall, ...] = ()
            runtime_session = None
            if failure is None and run is not None:
                calls = tuple(
                    await session.scalars(
                        select(ToolCall)
                        .where(
                            ToolCall.tenant_id == context.tenant_id,
                            ToolCall.run_id == run.id,
                        )
                        .order_by(ToolCall.created_at, ToolCall.id)
                    )
                )
                runtime_session = await session.scalar(
                    select(RuntimeSession).where(
                        RuntimeSession.tenant_id == context.tenant_id,
                        RuntimeSession.run_id == run.id,
                    )
                )
                failure = self._proof_trace_failure(
                    run,
                    calls,
                    runtime_session,
                    candidate_hash=candidate_hash,
                )
            now = datetime.now(UTC)
            proof = {
                "status": "failed" if failure else "succeeded",
                "run_id": str(command.run_id),
                "agent_version_id": str(version_id) if version_id is not None else None,
                "web_candidate_hash": candidate_hash,
                "verified_at": now.isoformat(),
                "failure_class": failure,
            }
            tenant.settings = merge_guided_setup_ledger(settings, proof=proof)
            tenant.revision += 1
            result = SetupProofRead(
                state="failed" if failure else "succeeded",
                tenant_revision=tenant.revision,
                run_id=command.run_id,
                agent_version_id=version_id,
                web_candidate_hash=candidate_hash,
                verified_at=now,
                failure_class=failure,
            )
            session.add(
                AuditRecord(
                    tenant_id=context.tenant_id,
                    action="guided_setup.proof.validate",
                    resource_type="run",
                    resource_id=command.run_id,
                    actor_id=context.actor_id,
                    details={
                        "state": result.state,
                        "failure_class": failure,
                        "agent_version_id": str(version_id) if version_id else None,
                        "web_candidate_hash": candidate_hash,
                        "tool_call_count": len(calls),
                    },
                    correlation_id=context.correlation_id,
                )
            )
            await session.flush()
            return result

    @staticmethod
    def _proof_failure(
        run: Run | None,
        *,
        expected_agent_id: UUID | None,
        expected_version_id: UUID | None,
        candidate_hash: str | None,
    ) -> str | None:
        if run is None:
            return "run_not_found"
        if expected_agent_id is None or expected_version_id is None:
            return "capability_target_missing"
        if candidate_hash is None:
            return "web_candidate_missing"
        if run.agent_id != expected_agent_id or run.agent_version_id != expected_version_id:
            return "agent_version_mismatch"
        if run.status != "completed":
            return "run_not_completed"
        if run.max_steps > 12 or run.token_budget is None or run.token_budget > 8_000:
            return "resource_bounds_invalid"
        if run.timeout_seconds is None or run.timeout_seconds > 180:
            return "resource_bounds_invalid"
        if run.budgets.get("setup_proof") is not True or set(
            _strings(run.budgets.get("tool_allow"))
        ) != {"web.search@1.0.0", "web.fetch@1.1.0"}:
            return "proof_scope_invalid"
        return None

    @staticmethod
    def _proof_trace_failure(
        run: Run,
        calls: tuple[ToolCall, ...],
        runtime_session: RuntimeSession | None,
        *,
        candidate_hash: str | None,
    ) -> str | None:
        if runtime_session is None or candidate_hash is None:
            return "runtime_snapshot_missing"
        runtime_checkpoint = _mapping(runtime_session.checkpoint)
        checkpoint_usage = _mapping(runtime_checkpoint.get("usage"))
        if (
            runtime_checkpoint.get("execution_mode") != "setup_proof"
            or runtime_checkpoint.get("loop_state") != "completed"
            or checkpoint_usage.get("model_calls") != 0
            or checkpoint_usage.get("tool_calls") != 2
        ):
            return "platform_orchestration_missing"
        snapshot_tools = _mapping(runtime_session.tool_policy_snapshot.get("tools"))
        if set(_strings(runtime_session.tool_policy_snapshot.get("allow"))) != {
            "web.search@1.0.0",
            "web.fetch@1.1.0",
        }:
            return "unexpected_tool_authorization"
        search_config = _mapping(snapshot_tools.get("web.search@1.0.0"))
        fetch_config = _mapping(snapshot_tools.get("web.fetch@1.1.0"))
        if (
            search_config.get("candidate_hash") != candidate_hash
            or fetch_config.get("candidate_hash") != candidate_hash
        ):
            return "web_candidate_mismatch"
        if [call.tool_name for call in calls] != ["web.search", "web.fetch"]:
            return "unexpected_tool_call"
        if any(call.status != "succeeded" for call in calls):
            return "tool_call_failed"
        searches = {call.id: call for call in calls if call.tool_name == "web.search"}
        fetches = [call for call in calls if call.tool_name == "web.fetch"]
        if not searches:
            return "search_missing"
        if not fetches:
            return "fetch_missing"
        observed_urls: list[str] = []
        for fetch in fetches:
            search_id = _uuid(fetch.arguments.get("search_tool_call_id"))
            search = searches.get(search_id)
            if search is None or search.created_at > fetch.created_at:
                return "fetch_source_unauthorized"
            allowed_urls = _result_urls(search.result)
            requested = _canonical_url(fetch.arguments.get("url"))
            final_url = _canonical_url(_mapping(fetch.result).get("final_url"))
            if requested is None or requested not in allowed_urls or final_url is None:
                return "fetch_source_unauthorized"
            observed_urls.append(final_url)
        if not output_has_observed_web_citation(run.result or {}, tuple(observed_urls)):
            return "citation_missing"
        return None

    def _model_area(
        self,
        ready: bool,
        latest_probe: ProviderProbe | None,
    ) -> SetupAreaRead:
        if ready:
            return SetupAreaRead(
                key="model", state="ready", summary="A verified Nico native model route is active"
            )
        if latest_probe is not None and latest_probe.status == "failed":
            return SetupAreaRead(
                key="model",
                state="failed",
                summary="The latest model verification failed",
                next_action="retry model Provider setup",
                details={"error_code": latest_probe.error_code or "PROVIDER_PROBE_FAILED"},
            )
        if not self.settings.model_endpoint_writes_enabled:
            return SetupAreaRead(
                key="model",
                state="blocked",
                summary="Model Provider activation is disabled by deployment policy",
                next_action="enable model Provider writes",
            )
        return SetupAreaRead(
            key="model",
            state="incomplete",
            summary="No verified Nico native model route is active",
            next_action="configure a model Provider",
        )

    def _web_area(
        self,
        ledger: dict[str, Any],
        configured: dict[str, Any],
        latest_probe: ProviderProbe | None,
    ) -> SetupAreaRead:
        provider = configured.get("provider")
        active = (
            provider in {"brave", "searxng"}
            and configured.get("enabled") is True
            and _hash(configured.get("candidate_hash")) is not None
            and latest_probe is not None
            and latest_probe.status in {"succeeded", "activated"}
        )
        if active:
            return SetupAreaRead(
                key="web",
                state="ready",
                summary=f"{provider} Web Provider is verified and active",
                details={"provider": str(provider)},
            )
        if ledger.get("web_intent") == "skipped":
            return SetupAreaRead(
                key="web",
                state="skipped",
                summary="Web setup was intentionally skipped",
                next_action="resume Web Provider setup",
            )
        if latest_probe is not None and latest_probe.status == "failed":
            return SetupAreaRead(
                key="web",
                state="failed",
                summary="The latest Web Provider verification failed",
                next_action="retry Web Provider setup",
                details={"error_code": latest_probe.error_code or "WEB_PROBE_FAILED"},
            )
        if not self.settings.web_provider_writes_enabled:
            return SetupAreaRead(
                key="web",
                state="blocked",
                summary="Web Provider activation is disabled by deployment policy",
                next_action="enable Web Provider writes",
            )
        return SetupAreaRead(
            key="web",
            state="incomplete",
            summary="No verified Web Provider is active",
            next_action="configure or skip Web",
        )

    async def _proof_area(
        self,
        session: Any,
        tenant_id: UUID,
        proof: dict[str, Any],
        agent_version_id: UUID | None,
        web_candidate_hash: str | None,
        *,
        web_ready: bool,
        web_authorized: bool,
    ) -> tuple[SetupAreaRead, datetime | None]:
        proof_run_id = _uuid(proof.get("run_id"))
        proof_version_id = _uuid(proof.get("agent_version_id"))
        proof_hash = _hash(proof.get("web_candidate_hash"))
        verified_at = _datetime(proof.get("verified_at"))
        fingerprint_matches = (
            proof.get("status") == "succeeded"
            and proof_run_id is not None
            and proof_version_id == agent_version_id
            and proof_hash is not None
            and proof_hash == web_candidate_hash
        )
        if fingerprint_matches:
            run = await session.scalar(
                select(Run).where(
                    Run.tenant_id == tenant_id,
                    Run.id == proof_run_id,
                    Run.agent_version_id == proof_version_id,
                    Run.status == "completed",
                )
            )
            if run is not None:
                return (
                    SetupAreaRead(
                        key="verification",
                        state="ready",
                        summary="Search, Fetch, and citation verification passed",
                        details={"run_id": str(run.id)},
                    ),
                    verified_at,
                )
        if proof.get("status") == "failed":
            return (
                SetupAreaRead(
                    key="verification",
                    state="failed",
                    summary="The latest online verification failed",
                    next_action="retry online verification",
                    details={"failure_class": str(proof.get("failure_class") or "unknown")},
                ),
                None,
            )
        if web_ready and not web_authorized:
            return (
                SetupAreaRead(
                    key="verification",
                    state="blocked",
                    summary=(
                        "The selected AgentVersion does not authorize Search and Fetch "
                        "for the current Web Provider"
                    ),
                    next_action="publish Web Research or Custom Search and Fetch capabilities",
                    details={"reason_code": "AGENT_WEB_CAPABILITIES_REQUIRED"},
                ),
                None,
            )
        return (
            SetupAreaRead(
                key="verification",
                state="incomplete",
                summary=(
                    "Online verification has not completed"
                    if web_ready
                    else "Online verification requires a ready Web Provider"
                ),
                next_action="run Search to Fetch verification" if web_ready else None,
            ),
            None,
        )


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _version_web_authorized(
    version: AgentVersion,
    candidate_hash: str | None,
) -> bool:
    if candidate_hash is None:
        return False
    policy = _mapping(version.tool_policy)
    required = {"web.search@1.0.0", "web.fetch@1.1.0"}
    if not required <= set(_strings(policy.get("allow"))):
        return False
    tools = _mapping(policy.get("tools"))
    return all(
        _mapping(tools.get(reference)).get("candidate_hash") == candidate_hash
        for reference in required
    )


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _ledger_matches(ledger: dict[str, Any], changes: dict[str, object]) -> bool:
    for key, value in changes.items():
        normalized = str(value) if isinstance(value, UUID) else value
        if ledger.get(key) != normalized:
            return False
    return True


def _intent_read(revision: int, ledger: dict[str, Any]) -> SetupIntentRead:
    return SetupIntentRead(
        tenant_revision=revision,
        web_intent=ledger.get("web_intent"),
        selected_agent_id=_uuid(ledger.get("selected_agent_id")),
        selected_profile=ledger.get("selected_profile"),
        capability_agent_version_id=_uuid(ledger.get("capability_agent_version_id")),
    )


def _uuid(value: object) -> UUID | None:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _hash(value: object) -> str | None:
    if isinstance(value, str) and len(value) == 64:
        try:
            int(value, 16)
        except ValueError:
            return None
        return value
    return None


def _datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _normalized_proof(value: dict[str, Any]) -> dict[str, Any] | None:
    status = value.get("status")
    if status not in {"succeeded", "failed"}:
        return None
    normalized: dict[str, Any] = {"status": status}
    for key in ("run_id", "agent_version_id"):
        parsed = _uuid(value.get(key))
        if parsed is not None:
            normalized[key] = str(parsed)
    candidate_hash = _hash(value.get("web_candidate_hash"))
    if candidate_hash is not None:
        normalized["web_candidate_hash"] = candidate_hash
    verified_at = _datetime(value.get("verified_at"))
    if verified_at is not None:
        normalized["verified_at"] = verified_at.isoformat()
    failure_class = value.get("failure_class")
    if isinstance(failure_class, str) and 0 < len(failure_class) <= 80:
        normalized["failure_class"] = failure_class
    return normalized


def _canonical_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return canonicalize_http_url(value).url
    except SafeHttpError:
        return None


def _result_urls(value: object) -> set[str]:
    result = _mapping(value)
    rows = result.get("results")
    if not isinstance(rows, list):
        return set()
    urls: set[str] = set()
    for row in rows[:10]:
        url = _canonical_url(_mapping(row).get("url"))
        if url is not None:
            urls.add(url)
    return urls
