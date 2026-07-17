"""Provider-neutral deterministic validation and immutable Evaluation persistence."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, Protocol, runtime_checkable
from uuid import UUID

from jsonschema import validators
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import DomainConflict, ResourceNotFound
from nico_agent.domain.models import (
    AuditRecord,
    Evaluation,
    Event,
    GrowthSource,
    Memory,
    Skill,
    SkillVersion,
    ToolDefinition,
)
from nico_agent.growth.contracts import canonical_hash, skill_content_hash
from nico_agent.memory.chunking import content_hash, normalize_text

SubjectType = Literal["memory", "skill_version"]


class ValidationSource(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    run_id: UUID
    run_step_id: UUID
    tool_call_id: UUID | None
    runtime_session_id: UUID | None
    agent_version_id: UUID
    trajectory_hash: str
    generator_name: str
    generator_version: str
    source_hash: str
    snapshot_bound: bool
    run_terminal: bool
    step_terminal: bool
    tool_calls_terminal: bool
    runtime_terminal: bool
    observed_tools: tuple[tuple[str, str, UUID], ...]


class ResolvedValidationTool(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str
    version: str
    status: str


class MemoryValidationPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    memory_key: UUID
    version: int
    memory_type: str
    scope_type: str
    project_id: UUID | None
    agent_id: UUID | None
    status: str
    content: str
    confidence: float
    content_hash: str
    supersedes_id: UUID | None
    expires_at: datetime | None


class SkillValidationPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    skill_id: UUID
    skill_status: str
    version: int
    status: str
    conditions: dict[str, Any]
    preconditions: tuple[dict[str, Any], ...]
    input_schema: dict[str, Any]
    steps: tuple[dict[str, Any], ...]
    tools: tuple[dict[str, Any], ...]
    output_schema: dict[str, Any]
    validation: dict[str, Any]
    failure_modes: tuple[dict[str, Any], ...]
    content_hash: str


class ValidationSubject(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: UUID
    subject_type: SubjectType
    subject_id: UUID
    content_hash: str
    memory: MemoryValidationPayload | None = None
    skill_version: SkillValidationPayload | None = None
    sources: tuple[ValidationSource, ...]
    resolved_tools: tuple[ResolvedValidationTool, ...] = ()
    snapshot_hash: str

    @model_validator(mode="after")
    def validate_shape(self) -> ValidationSubject:
        if self.subject_type == "memory" and (
            self.memory is None or self.skill_version is not None
        ):
            raise ValueError("memory validation subject has an invalid shape")
        if self.subject_type == "skill_version" and (
            self.skill_version is None or self.memory is not None
        ):
            raise ValueError("skill validation subject has an invalid shape")
        return self


class ValidationCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str = Field(min_length=1, max_length=120)
    passed: bool
    message: str = Field(min_length=1, max_length=2_000)
    evidence: dict[str, Any] = Field(default_factory=dict)


class ValidationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    verdict: Literal["pass", "fail"]
    score: float = Field(ge=0, le=1)
    checks: tuple[ValidationCheck, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_verdict(self) -> ValidationResult:
        all_passed = all(check.passed for check in self.checks)
        if (self.verdict == "pass") is not all_passed:
            raise ValueError("validation verdict must match its checks")
        return self


@runtime_checkable
class GrowthValidator(Protocol):
    name: str
    version: str

    async def validate(self, subject: ValidationSubject) -> ValidationResult: ...


class EvaluationReference(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    subject_type: SubjectType
    subject_id: UUID
    content_hash: str
    evaluator_name: str
    evaluator_version: str
    status: str
    score: float | None
    verdict: str | None
    revision: int
    already_evaluated: bool


class ValidationSubjectBuilder:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def build(
        self, context: TenantContext, subject_type: SubjectType, subject_id: UUID
    ) -> ValidationSubject:
        async with self.database.tenant_transaction(context) as session:
            return await self.build_in_session(session, subject_type, subject_id)

    async def build_in_session(
        self,
        session: AsyncSession,
        subject_type: SubjectType,
        subject_id: UUID,
        *,
        for_update: bool = False,
    ) -> ValidationSubject:
        if subject_type == "memory":
            statement = select(Memory).where(Memory.id == subject_id)
            if for_update:
                statement = statement.with_for_update()
            memory = await session.scalar(statement)
            if memory is None:
                raise ResourceNotFound("memory", str(subject_id))
            payload: dict[str, Any] = {
                "tenant_id": memory.tenant_id,
                "subject_type": subject_type,
                "subject_id": memory.id,
                "content_hash": memory.content_hash,
                "memory": MemoryValidationPayload(
                    id=memory.id,
                    memory_key=memory.memory_key,
                    version=memory.version,
                    memory_type=memory.memory_type,
                    scope_type=memory.scope_type,
                    project_id=memory.project_id,
                    agent_id=memory.agent_id,
                    status=memory.status,
                    content=memory.content,
                    confidence=memory.confidence,
                    content_hash=memory.content_hash,
                    supersedes_id=memory.supersedes_id,
                    expires_at=memory.expires_at,
                ),
                "skill_version": None,
                "resolved_tools": (),
            }
        else:
            statement = select(SkillVersion).where(SkillVersion.id == subject_id)
            if for_update:
                statement = statement.with_for_update()
            version = await session.scalar(statement)
            if version is None:
                raise ResourceNotFound("skill_version", str(subject_id))
            skill = await session.scalar(select(Skill).where(Skill.id == version.skill_id))
            if skill is None:
                raise DomainConflict("GROWTH_SUBJECT_BROKEN", "skill identity is missing")
            resolved_tools = await self._resolved_tools(session, version.tools)
            payload = {
                "tenant_id": version.tenant_id,
                "subject_type": subject_type,
                "subject_id": version.id,
                "content_hash": version.content_hash,
                "memory": None,
                "skill_version": SkillValidationPayload(
                    id=version.id,
                    skill_id=version.skill_id,
                    skill_status=skill.status,
                    version=version.version,
                    status=version.status,
                    conditions=version.conditions,
                    preconditions=tuple(version.preconditions),
                    input_schema=version.input_schema,
                    steps=tuple(version.steps),
                    tools=tuple(version.tools),
                    output_schema=version.output_schema,
                    validation=version.validation,
                    failure_modes=tuple(version.failure_modes),
                    content_hash=version.content_hash,
                ),
                "resolved_tools": resolved_tools,
            }

        sources = tuple(
            self._source_evidence(source)
            for source in (
                await session.scalars(
                    select(GrowthSource)
                    .where(
                        GrowthSource.subject_type == subject_type,
                        (
                            GrowthSource.memory_id == subject_id
                            if subject_type == "memory"
                            else GrowthSource.skill_version_id == subject_id
                        ),
                    )
                    .order_by(GrowthSource.created_at, GrowthSource.id)
                )
            ).all()
        )
        payload["sources"] = sources
        return ValidationSubject(**payload, snapshot_hash=canonical_hash(payload))

    @staticmethod
    async def _resolved_tools(
        session: AsyncSession, tools: list[dict[str, Any]]
    ) -> tuple[ResolvedValidationTool, ...]:
        ids: set[UUID] = set()
        for item in tools:
            try:
                ids.add(UUID(str(item.get("tool_definition_id"))))
            except (TypeError, ValueError, AttributeError):
                continue
        if not ids:
            return ()
        definitions = (
            await session.scalars(
                select(ToolDefinition)
                .where(ToolDefinition.id.in_(ids))
                .order_by(ToolDefinition.name, ToolDefinition.version, ToolDefinition.id)
            )
        ).all()
        return tuple(
            ResolvedValidationTool(
                id=item.id,
                name=item.name,
                version=item.version,
                status=item.status,
            )
            for item in definitions
        )

    @staticmethod
    def _source_evidence(source: GrowthSource) -> ValidationSource:
        trajectory = source.snapshot.get("trajectory", {})
        steps = trajectory.get("steps", []) if isinstance(trajectory, dict) else []
        observed: set[tuple[str, str, UUID]] = set()
        step_terminal = bool(steps)
        tool_calls_terminal = True
        for step in steps if isinstance(steps, list) else []:
            if not isinstance(step, dict) or step.get("status") not in {
                "completed",
                "failed",
                "cancelled",
            }:
                step_terminal = False
                continue
            for call in step.get("tool_calls", []):
                if not isinstance(call, dict):
                    tool_calls_terminal = False
                    continue
                if call.get("status") not in {
                    "succeeded",
                    "failed",
                    "timed_out",
                    "cancelled",
                }:
                    tool_calls_terminal = False
                try:
                    observed.add(
                        (
                            str(call["name"]),
                            str(call["version"]),
                            UUID(str(call["tool_definition_id"])),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    tool_calls_terminal = False
        runtime = trajectory.get("runtime") if isinstance(trajectory, dict) else None
        runtime_terminal = runtime is None or (
            isinstance(runtime, dict)
            and runtime.get("status") in {"completed", "failed", "cancelled"}
        )
        return ValidationSource(
            id=source.id,
            run_id=source.run_id,
            run_step_id=source.run_step_id,
            tool_call_id=source.tool_call_id,
            runtime_session_id=source.runtime_session_id,
            agent_version_id=source.agent_version_id,
            trajectory_hash=source.trajectory_hash,
            generator_name=source.generator_name,
            generator_version=source.generator_version,
            source_hash=source.source_hash,
            snapshot_bound=(
                source.snapshot.get("candidate_key") == source.source_hash
                and isinstance(trajectory, dict)
                and trajectory.get("snapshot_hash") == source.trajectory_hash
            ),
            run_terminal=(
                isinstance(trajectory, dict)
                and trajectory.get("run_status")
                in {"completed", "failed", "cancelled", "timed_out"}
            ),
            step_terminal=step_terminal,
            tool_calls_terminal=tool_calls_terminal,
            runtime_terminal=runtime_terminal,
            observed_tools=tuple(sorted(observed, key=lambda item: (item[0], item[1], item[2]))),
        )


class DeterministicGrowthValidator:
    name = "nico-deterministic-growth-validator"
    version = "1.0.0"

    async def validate(self, subject: ValidationSubject) -> ValidationResult:
        checks = (
            self._validate_memory(subject)
            if subject.subject_type == "memory"
            else self._validate_skill(subject)
        )
        passed = sum(check.passed for check in checks)
        return ValidationResult(
            verdict="pass" if passed == len(checks) else "fail",
            score=passed / len(checks),
            checks=checks,
        )

    def _validate_memory(self, subject: ValidationSubject) -> tuple[ValidationCheck, ...]:
        memory = subject.memory
        assert memory is not None
        try:
            normalized = normalize_text(memory.content)
            hash_matches = content_hash(normalized) == memory.content_hash
            normalized_content = normalized == memory.content
        except (TypeError, ValueError):
            hash_matches = False
            normalized_content = False
        scope_valid = (
            (
                memory.scope_type == "tenant"
                and memory.project_id is None
                and memory.agent_id is None
            )
            or (
                memory.scope_type == "project"
                and memory.project_id is not None
                and memory.agent_id is None
            )
            or (
                memory.scope_type == "agent"
                and memory.project_id is None
                and memory.agent_id is not None
            )
        )
        version_valid = (memory.version == 1 and memory.supersedes_id is None) or (
            memory.version > 1 and memory.supersedes_id is not None
        )
        return (
            self._check("candidate_status", memory.status == "candidate", memory.status),
            self._check("content_hash", hash_matches, memory.content_hash),
            self._check("normalized_content", normalized_content, "NFKC"),
            self._check("scope_shape", scope_valid, memory.scope_type),
            self._check("version_chain", version_valid, str(memory.version)),
            self._check(
                "working_expiry",
                memory.memory_type != "working" or memory.expires_at is not None,
                memory.memory_type,
            ),
            *self._source_checks(subject),
        )

    def _validate_skill(self, subject: ValidationSubject) -> tuple[ValidationCheck, ...]:
        version = subject.skill_version
        assert version is not None
        declared = self._declared_tools(version.tools)
        resolved = {
            (item.name, item.version, item.id): item.status for item in subject.resolved_tools
        }
        observed = {item for source in subject.sources for item in source.observed_tools}
        tool_shape = declared is not None
        declared_set = set(declared or ())
        tools_resolved = tool_shape and all(
            resolved.get(item) == "enabled" for item in declared_set
        )
        tools_observed = tool_shape and declared_set.issubset(observed)
        step_ids = [str(item.get("id", "")).strip() for item in version.steps]
        steps_valid = bool(step_ids) and all(step_ids) and len(step_ids) == len(set(step_ids))
        return (
            self._check(
                "candidate_status",
                version.status in {"draft", "testing"}
                and version.skill_status in {"candidate", "testing"},
                f"{version.skill_status}/{version.status}",
            ),
            self._check(
                "content_hash",
                skill_content_hash(version) == version.content_hash,
                version.content_hash,
            ),
            self._check("input_schema", self._valid_schema(version.input_schema), "JSON Schema"),
            self._check("output_schema", self._valid_schema(version.output_schema), "JSON Schema"),
            self._check("unique_steps", steps_valid, str(len(version.steps))),
            self._check("tool_shape", tool_shape, str(len(version.tools))),
            self._check("tools_enabled", tools_resolved, str(len(resolved))),
            self._check("tools_observed", tools_observed, str(len(observed))),
            self._check(
                "failure_modes", bool(version.failure_modes), str(len(version.failure_modes))
            ),
            *self._source_checks(subject),
        )

    @staticmethod
    def _source_checks(subject: ValidationSubject) -> tuple[ValidationCheck, ...]:
        sources = subject.sources
        return (
            DeterministicGrowthValidator._check("source_present", bool(sources), str(len(sources))),
            DeterministicGrowthValidator._check(
                "source_binding",
                bool(sources) and all(item.snapshot_bound for item in sources),
                "hash",
            ),
            DeterministicGrowthValidator._check(
                "terminal_trajectory",
                bool(sources)
                and all(
                    item.run_terminal
                    and item.step_terminal
                    and item.tool_calls_terminal
                    and item.runtime_terminal
                    for item in sources
                ),
                "terminal",
            ),
        )

    @staticmethod
    def _declared_tools(
        tools: tuple[dict[str, Any], ...],
    ) -> tuple[tuple[str, str, UUID], ...] | None:
        result: list[tuple[str, str, UUID]] = []
        try:
            for item in tools:
                result.append(
                    (
                        str(item["name"]),
                        str(item["version"]),
                        UUID(str(item["tool_definition_id"])),
                    )
                )
        except (KeyError, TypeError, ValueError):
            return None
        return tuple(result) if len(result) == len(set(result)) else None

    @staticmethod
    def _valid_schema(schema: dict[str, Any]) -> bool:
        try:
            validator = validators.validator_for(schema)
            validator.check_schema(schema)
        except Exception:
            return False
        return True

    @staticmethod
    def _check(code: str, passed: bool, evidence: str) -> ValidationCheck:
        return ValidationCheck(
            code=code,
            passed=passed,
            message=f"{code} {'passed' if passed else 'failed'}",
            evidence={"value": evidence[:500]},
        )


class GrowthEvaluationService:
    def __init__(
        self,
        database: Database,
        *,
        validator: GrowthValidator | None = None,
        subject_builder: ValidationSubjectBuilder | None = None,
    ) -> None:
        self.database = database
        self.validator = validator or DeterministicGrowthValidator()
        self.subject_builder = subject_builder or ValidationSubjectBuilder(database)
        if not self.validator.name or not self.validator.version:
            raise ValueError("growth validator requires stable name and version")
        if len(self.validator.name) > 120 or len(self.validator.version) > 80:
            raise ValueError("growth validator identity exceeds persistence limits")
        if len(self._actor) > 200:
            raise ValueError("growth validator identity exceeds actor persistence limits")

    async def evaluate(
        self, context: TenantContext, subject_type: SubjectType, subject_id: UUID
    ) -> EvaluationReference:
        subject = await self.subject_builder.build(context, subject_type, subject_id)
        existing = await self._find_existing(context, subject)
        if existing is not None:
            return self._reference(existing, already_evaluated=True)
        self._require_validatable(subject)

        validation_started_at = datetime.now(UTC)
        result: ValidationResult | None = None
        provider_error: dict[str, Any] | None = None
        try:
            result = ValidationResult.model_validate(await self.validator.validate(subject))
        except Exception as exc:
            provider_error = {"code": "VALIDATOR_ERROR", "type": type(exc).__name__}

        async with self.database.tenant_transaction(context) as session:
            locked = await self.subject_builder.build_in_session(
                session, subject_type, subject_id, for_update=True
            )
            if locked.snapshot_hash != subject.snapshot_hash:
                raise DomainConflict(
                    "VALIDATION_SUBJECT_CHANGED",
                    "growth subject changed while validation was running",
                    details={"subject_id": str(subject_id)},
                )
            existing = await self._find_existing_in_session(session, locked)
            if existing is not None:
                return self._reference(existing, already_evaluated=True)
            self._require_validatable(locked)

            now = datetime.now(UTC)
            evaluation = Evaluation(
                tenant_id=context.tenant_id,
                subject_type=subject_type,
                memory_id=subject_id if subject_type == "memory" else None,
                skill_version_id=subject_id if subject_type == "skill_version" else None,
                evaluator_name=self.validator.name,
                evaluator_version=self.validator.version,
                content_hash=locked.content_hash,
                status="pending",
                created_by=self._actor,
                started_at=validation_started_at,
            )
            session.add(evaluation)
            await session.flush()
            if provider_error is not None:
                evaluation.status = "failed"
                evaluation.error = provider_error
                event_type = "GrowthEvaluationFailed"
            else:
                assert result is not None
                evaluation.status = "completed"
                evaluation.score = result.score
                evaluation.verdict = result.verdict
                evaluation.details = {
                    "subject_snapshot_hash": locked.snapshot_hash,
                    "checks": {item.code: item.passed for item in result.checks},
                }
                evaluation.evidence = [item.model_dump(mode="json") for item in result.checks]
                event_type = "GrowthEvaluationCompleted"
            evaluation.ended_at = now
            evaluation.revision += 1
            self._record(
                session,
                context,
                evaluation,
                subject_id,
                event_type=event_type,
            )
            await session.flush()
            return self._reference(evaluation, already_evaluated=False)

    async def _find_existing(
        self, context: TenantContext, subject: ValidationSubject
    ) -> Evaluation | None:
        async with self.database.tenant_transaction(context) as session:
            return await self._find_existing_in_session(session, subject)

    async def _find_existing_in_session(
        self, session: AsyncSession, subject: ValidationSubject
    ) -> Evaluation | None:
        return await session.scalar(
            select(Evaluation)
            .where(
                Evaluation.subject_type == subject.subject_type,
                (
                    Evaluation.memory_id == subject.subject_id
                    if subject.subject_type == "memory"
                    else Evaluation.skill_version_id == subject.subject_id
                ),
                Evaluation.evaluator_name == self.validator.name,
                Evaluation.evaluator_version == self.validator.version,
                Evaluation.content_hash == subject.content_hash,
            )
            .order_by(Evaluation.created_at.desc(), Evaluation.id.desc())
        )

    @staticmethod
    def _require_validatable(subject: ValidationSubject) -> None:
        if subject.subject_type == "memory":
            assert subject.memory is not None
            valid = subject.memory.status == "candidate"
            status = subject.memory.status
        else:
            assert subject.skill_version is not None
            valid = subject.skill_version.status in {"draft", "testing"}
            status = subject.skill_version.status
        if not valid:
            raise DomainConflict(
                "GROWTH_SUBJECT_NOT_VALIDATABLE",
                "growth subject is not in a candidate validation state",
                details={"subject_id": str(subject.subject_id), "status": status},
            )

    @property
    def _actor(self) -> str:
        return f"evaluator:{self.validator.name}@{self.validator.version}"

    @staticmethod
    def _reference(evaluation: Evaluation, *, already_evaluated: bool) -> EvaluationReference:
        subject_id = evaluation.memory_id or evaluation.skill_version_id
        assert subject_id is not None
        return EvaluationReference(
            id=evaluation.id,
            subject_type=evaluation.subject_type,
            subject_id=subject_id,
            content_hash=evaluation.content_hash,
            evaluator_name=evaluation.evaluator_name,
            evaluator_version=evaluation.evaluator_version,
            status=evaluation.status,
            score=evaluation.score,
            verdict=evaluation.verdict,
            revision=evaluation.revision,
            already_evaluated=already_evaluated,
        )

    def _record(
        self,
        session: AsyncSession,
        context: TenantContext,
        evaluation: Evaluation,
        subject_id: UUID,
        *,
        event_type: str,
    ) -> None:
        payload = {
            "evaluation_id": str(evaluation.id),
            "content_hash": evaluation.content_hash,
            "status": evaluation.status,
            "verdict": evaluation.verdict,
            "evaluator": f"{evaluation.evaluator_name}@{evaluation.evaluator_version}",
        }
        session.add_all(
            [
                Event(
                    tenant_id=context.tenant_id,
                    event_type=event_type,
                    aggregate_type=evaluation.subject_type,
                    aggregate_id=subject_id,
                    actor_id=self._actor,
                    payload=payload,
                    correlation_id=context.correlation_id,
                ),
                AuditRecord(
                    tenant_id=context.tenant_id,
                    action=(
                        "growth.evaluation.fail"
                        if evaluation.status == "failed"
                        else "growth.evaluation.complete"
                    ),
                    resource_type=evaluation.subject_type,
                    resource_id=subject_id,
                    actor_id=self._actor,
                    details=payload,
                    correlation_id=context.correlation_id,
                ),
            ]
        )
