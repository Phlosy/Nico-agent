from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from nico_agent.config import Settings
from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import DomainError
from nico_agent.domain.models import (
    Agent,
    AgentVersion,
    Approval,
    AuditRecord,
    Evaluation,
    Event,
    GrowthSource,
    Memory,
    MemoryChunk,
    Project,
    Run,
    RunStep,
    Skill,
    SkillVersion,
    Task,
    Tenant,
)
from nico_agent.growth import (
    GrowthApprovalService,
    GrowthCandidateService,
    GrowthEvaluationService,
)
from nico_agent.growth.evaluation import ValidationResult, ValidationSubject
from nico_agent.memory.contracts import MemoryQueryContext
from nico_agent.memory.lifecycle import MemoryLifecycleService
from nico_agent.memory.service import MemoryRetriever

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with the Compose dependencies running",
)


def _database() -> tuple[Database, AsyncEngine]:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    return Database(engine), engine


def _context(scope: dict, actor: str) -> TenantContext:
    return TenantContext(scope["tenant_id"], actor, uuid4())


async def _seed_candidates(database: Database, label: str) -> tuple[dict, object]:
    suffix = uuid4().hex[:10]
    async with database.admin_transaction() as session:
        tenant = Tenant(name=f"Review {label} {suffix}", slug=f"review-{label}-{suffix}")
        session.add(tenant)
        await session.flush()
        project = Project(tenant_id=tenant.id, name=f"project-{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            name=f"agent-{suffix}",
            display_name="Review Agent",
        )
        session.add_all([project, agent])
        await session.flush()
        version = AgentVersion(
            tenant_id=tenant.id,
            agent_id=agent.id,
            version=1,
            status="published",
            role="growth-review-test",
            mandate="Create reviewable evidence",
            content_hash="a" * 64,
        )
        session.add(version)
        await session.flush()
        agent.current_version_id = version.id
        agent.status = "ready"
        task = Task(
            tenant_id=tenant.id,
            project_id=project.id,
            assignee_agent_id=agent.id,
            title="Review controlled growth",
            input={"topic": "candidate governance"},
            acceptance={"requires_human_review": True},
            status="completed",
        )
        session.add(task)
        await session.flush()
        run = Run(
            tenant_id=tenant.id,
            task_id=task.id,
            agent_id=agent.id,
            agent_version_id=version.id,
            attempt=1,
            status="completed",
            result={"summary": "candidate governance requires independent review"},
            ended_at=datetime.now(UTC),
        )
        session.add(run)
        await session.flush()
        step = RunStep(
            tenant_id=tenant.id,
            run_id=run.id,
            sequence=1,
            kind="result",
            status="completed",
            output={"lesson": "validate, review, then publish"},
            ended_at=datetime.now(UTC),
        )
        session.add(step)
        await session.flush()
        scope = {
            "tenant_id": tenant.id,
            "project_id": project.id,
            "agent_id": agent.id,
            "run_id": run.id,
        }
    generated = await GrowthCandidateService(database).generate(
        _context(scope, "reflection:test"), run.id
    )
    return scope, generated


def _semantic(generated):
    return next(item for item in generated.memories if item.memory_type == "semantic")


async def _evaluate_approve_publish(
    database: Database,
    scope: dict,
    memory_id,
    *,
    memory_revision: int = 1,
):
    evaluation = await GrowthEvaluationService(database).evaluate(
        _context(scope, "evaluator:caller"), "memory", memory_id
    )
    assert evaluation.verdict == "pass"
    request = await GrowthApprovalService(database).request(
        _context(scope, "operator:requester"),
        "memory",
        memory_id,
        expected_revision=memory_revision,
    )
    approval = await GrowthApprovalService(database).decide(
        _context(scope, "operator:reviewer"),
        request.id,
        decision="approved",
        reason="Independent deterministic validation passed.",
        expected_revision=request.revision,
    )
    assert approval.status == "approved"
    return await MemoryLifecycleService(database).publish(
        _context(scope, "operator:publisher"),
        memory_id,
        expected_revision=memory_revision,
    )


@pytest.mark.asyncio
async def test_memory_review_publish_is_concurrent_idempotent_and_retrievable() -> None:
    database, engine = _database()
    try:
        scope, generated = await _seed_candidates(database, "publish")
        memory = _semantic(generated)
        evaluation_service = GrowthEvaluationService(database)
        evaluator_context = _context(scope, "evaluator:caller")

        async with database.admin_transaction() as session:
            other = Tenant(
                name=f"Other Review {uuid4().hex[:8]}",
                slug=f"other-review-{uuid4().hex[:8]}",
            )
            session.add(other)
            await session.flush()
            other_context = TenantContext(other.id, "other:evaluator", uuid4())
        with pytest.raises(DomainError) as hidden:
            await evaluation_service.evaluate(other_context, "memory", memory.id)
        assert hidden.value.code == "RESOURCE_NOT_FOUND"

        first, second = await asyncio.gather(
            evaluation_service.evaluate(evaluator_context, "memory", memory.id),
            evaluation_service.evaluate(evaluator_context, "memory", memory.id),
        )
        assert {first.already_evaluated, second.already_evaluated} == {False, True}
        assert first.id == second.id
        assert first.verdict == second.verdict == "pass"

        with pytest.raises(DomainError) as unapproved_publish:
            await MemoryLifecycleService(database).publish(
                _context(scope, "operator:publisher"),
                memory.id,
                expected_revision=1,
            )
        assert unapproved_publish.value.code == "GROWTH_APPROVAL_REQUIRED"

        approval_service = GrowthApprovalService(database)
        requester = _context(scope, "operator:requester")
        request, repeated = await asyncio.gather(
            approval_service.request(requester, "memory", memory.id, expected_revision=1),
            approval_service.request(requester, "memory", memory.id, expected_revision=1),
        )
        assert repeated.id == request.id
        assert {request.already_exists, repeated.already_exists} == {False, True}

        with pytest.raises(DomainError) as self_review:
            await approval_service.decide(
                requester,
                request.id,
                decision="approved",
                reason="Self review must be rejected.",
                expected_revision=request.revision,
            )
        assert self_review.value.code == "APPROVAL_SELF_REVIEW_FORBIDDEN"

        approval = await approval_service.decide(
            _context(scope, "operator:reviewer"),
            request.id,
            decision="approved",
            reason="Evidence and source binding passed independent review.",
            expected_revision=request.revision,
        )
        published = await MemoryLifecycleService(database).publish(
            _context(scope, "operator:publisher"),
            memory.id,
            expected_revision=1,
        )
        assert approval.status == "approved"
        assert published.status == "active"
        assert published.indexed is True
        assert published.chunk_count >= 1

        results = await MemoryRetriever(database).search(
            _context(scope, "runtime:reader"),
            MemoryQueryContext(project_id=scope["project_id"]),
            "candidate governance independent review",
        )
        assert memory.id in {item.memory_id for item in results}

        async with database.tenant_transaction(requester) as session:
            assert await session.scalar(select(func.count(Evaluation.id))) == 1
            assert await session.scalar(select(func.count(Approval.id))) == 1
            assert await session.scalar(select(func.count(MemoryChunk.id))) >= 1
            event_types = set((await session.scalars(select(Event.event_type))).all())
            audit_actions = set((await session.scalars(select(AuditRecord.action))).all())
        assert {
            "GrowthEvaluationCompleted",
            "ReviewRequested",
            "ReviewApproved",
            "MemoryPublished",
        }.issubset(event_types)
        assert {
            "growth.evaluation.complete",
            "growth.review.request",
            "growth.review.decide",
            "memory.publish",
        }.issubset(audit_actions)

        source_id = next(source.id for source in (await _sources(database, requester, memory.id)))
        with pytest.raises(DBAPIError):
            async with database.admin_transaction() as session:
                await session.execute(
                    text("UPDATE growth_sources SET generator_version = 'mutated' WHERE id = :id"),
                    {"id": source_id},
                )
    finally:
        await engine.dispose()


async def _sources(database: Database, context: TenantContext, memory_id):
    async with database.tenant_transaction(context) as session:
        return list(
            await session.scalars(select(GrowthSource).where(GrowthSource.memory_id == memory_id))
        )


class _BrokenValidator:
    name = "broken-validator"
    version = "1.0.0"

    async def validate(self, subject: ValidationSubject) -> ValidationResult:
        raise RuntimeError("sensitive provider detail must not be persisted")


@pytest.mark.asyncio
async def test_validator_failure_is_sanitized_persisted_and_blocks_review() -> None:
    database, engine = _database()
    try:
        scope, generated = await _seed_candidates(database, "validator-failure")
        memory = _semantic(generated)
        context = _context(scope, "evaluator:caller")

        passed = await GrowthEvaluationService(database).evaluate(context, "memory", memory.id)
        evaluation = await GrowthEvaluationService(database, validator=_BrokenValidator()).evaluate(
            context, "memory", memory.id
        )
        assert passed.verdict == "pass"
        assert evaluation.status == "failed"
        assert evaluation.verdict is None

        with pytest.raises(DomainError) as blocked:
            await GrowthApprovalService(database).request(
                _context(scope, "operator:requester"),
                "memory",
                memory.id,
                expected_revision=1,
            )
        assert blocked.value.code == "GROWTH_EVALUATION_NOT_PASSED"

        async with database.tenant_transaction(context) as session:
            stored = await session.get(Evaluation, evaluation.id)
            candidate = await session.get(Memory, memory.id)
        assert stored is not None
        assert stored.error == {"code": "VALIDATOR_ERROR", "type": "RuntimeError"}
        assert "sensitive provider detail" not in str(stored.error)
        assert candidate is not None and candidate.status == "candidate"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_skill_version_can_be_validated_and_approved_but_not_published_in_f5() -> None:
    database, engine = _database()
    try:
        scope, generated = await _seed_candidates(database, "skill-review")
        assert generated.skill is not None
        version_id = generated.skill.skill_version_id

        evaluation = await GrowthEvaluationService(database).evaluate(
            _context(scope, "evaluator:caller"), "skill_version", version_id
        )
        request = await GrowthApprovalService(database).request(
            _context(scope, "operator:requester"),
            "skill_version",
            version_id,
            expected_revision=1,
        )
        approval = await GrowthApprovalService(database).decide(
            _context(scope, "operator:reviewer"),
            request.id,
            decision="approved",
            reason="Skill schemas, steps and source tools are valid.",
            expected_revision=request.revision,
        )

        assert evaluation.verdict == "pass"
        assert approval.status == "approved"
        async with database.tenant_transaction(_context(scope, "reader")) as session:
            version = await session.get(SkillVersion, version_id)
            skill = await session.get(Skill, generated.skill.skill_id)
        assert version is not None and version.status == "draft"
        assert skill is not None and skill.status == "candidate"
        assert skill.current_version_id is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rejected_cancelled_and_expired_reviews_are_terminal_and_re_requestable() -> None:
    database, engine = _database()
    try:
        scope, generated = await _seed_candidates(database, "review-states")
        memory = _semantic(generated)
        await GrowthEvaluationService(database).evaluate(
            _context(scope, "evaluator:caller"), "memory", memory.id
        )
        service = GrowthApprovalService(database)
        requester = _context(scope, "operator:requester")
        reviewer = _context(scope, "operator:reviewer")

        first = await service.request(requester, "memory", memory.id, expected_revision=1)
        rejected = await service.decide(
            reviewer,
            first.id,
            decision="rejected",
            reason="Request needs a clearer rationale.",
            expected_revision=first.revision,
        )
        second = await service.request(requester, "memory", memory.id, expected_revision=1)
        assert rejected.status == "rejected"
        assert second.id != first.id

        with pytest.raises(DomainError) as forbidden:
            await service.cancel(
                reviewer,
                second.id,
                reason="Only requester may cancel.",
                expected_revision=second.revision,
            )
        assert forbidden.value.code == "APPROVAL_CANCEL_FORBIDDEN"
        cancelled = await service.cancel(
            requester,
            second.id,
            reason="Replace with a fresh request.",
            expected_revision=second.revision,
        )
        assert cancelled.status == "cancelled"

        async with database.admin_transaction() as session:
            stale = Approval(
                tenant_id=scope["tenant_id"],
                subject_type="memory",
                memory_id=memory.id,
                action="publish",
                content_hash=memory.content_hash,
                status="requested",
                requester="operator:old-requester",
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
            session.add(stale)
            await session.flush()
            stale_id = stale.id

        fresh = await service.request(requester, "memory", memory.id, expected_revision=1)
        assert fresh.status == "requested" and fresh.id != stale_id
        async with database.tenant_transaction(requester) as session:
            expired = await session.get(Approval, stale_id)
        assert expired is not None
        assert expired.status == "expired"
        assert expired.reason
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_memory_revision_stays_candidate_until_review_then_atomically_supersedes() -> None:
    database, engine = _database()
    try:
        scope, generated = await _seed_candidates(database, "revision")
        first = _semantic(generated)
        published = await _evaluate_approve_publish(database, scope, first.id)
        assert published.revision == 2

        lifecycle = MemoryLifecycleService(database)
        revised = await lifecycle.revise(
            _context(scope, "operator:editor"),
            first.id,
            content="Independent review is mandatory before any revised memory is recalled.",
            reason="Clarify the reusable governance rule.",
            expected_revision=published.revision,
            confidence=0.95,
        )
        assert revised.version == 2
        assert revised.status == "candidate"

        async with database.tenant_transaction(_context(scope, "reader")) as session:
            old = await session.get(Memory, first.id)
            new = await session.get(Memory, revised.id)
        assert old is not None and old.status == "active"
        assert new is not None and new.status == "candidate"

        await _evaluate_approve_publish(
            database,
            scope,
            revised.id,
            memory_revision=revised.revision,
        )
        async with database.tenant_transaction(_context(scope, "reader")) as session:
            old = await session.get(Memory, first.id)
            new = await session.get(Memory, revised.id)
        assert old is not None and old.status == "invalidated"
        assert new is not None and new.status == "active"

        results = await MemoryRetriever(database).search(
            _context(scope, "runtime:reader"),
            MemoryQueryContext(project_id=scope["project_id"]),
            "revised memory independent review mandatory",
        )
        assert revised.id in {item.memory_id for item in results}
        assert first.id not in {item.memory_id for item in results}

        with pytest.raises(DomainError) as stale:
            await lifecycle.revise(
                _context(scope, "operator:editor"),
                first.id,
                content="A stale branch must never create version three.",
                reason="Attempt stale revision.",
                expected_revision=old.revision,
            )
        assert stale.value.code == "MEMORY_REVISION_STALE"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_approved_older_revision_cannot_replace_a_newer_published_memory() -> None:
    database, engine = _database()
    try:
        scope, generated = await _seed_candidates(database, "publish-latest-only")
        first = _semantic(generated)
        published = await _evaluate_approve_publish(database, scope, first.id)
        lifecycle = MemoryLifecycleService(database)

        second = await lifecycle.revise(
            _context(scope, "operator:editor"),
            first.id,
            content="The second memory revision has passed independent human review.",
            reason="Create an independently reviewed second version.",
            expected_revision=published.revision,
        )
        await GrowthEvaluationService(database).evaluate(
            _context(scope, "evaluator:caller"), "memory", second.id
        )
        second_request = await GrowthApprovalService(database).request(
            _context(scope, "operator:requester"),
            "memory",
            second.id,
            expected_revision=second.revision,
        )
        await GrowthApprovalService(database).decide(
            _context(scope, "operator:reviewer"),
            second_request.id,
            decision="approved",
            reason="The second version is safe to publish.",
            expected_revision=second_request.revision,
        )

        third = await lifecycle.revise(
            _context(scope, "operator:editor"),
            second.id,
            content="The third and latest memory revision is the only publishable candidate.",
            reason="Supersede the reviewed candidate with a newer correction.",
            expected_revision=second.revision,
        )
        latest = await _evaluate_approve_publish(
            database,
            scope,
            third.id,
            memory_revision=third.revision,
        )
        assert latest.status == "active"

        with pytest.raises(DomainError) as stale_publish:
            await lifecycle.publish(
                _context(scope, "operator:publisher"),
                second.id,
                expected_revision=second.revision,
            )
        assert stale_publish.value.code == "MEMORY_VERSION_NOT_LATEST"

        async with database.tenant_transaction(_context(scope, "reader")) as session:
            stored_second = await session.get(Memory, second.id)
            stored_third = await session.get(Memory, third.id)
        assert stored_second is not None and stored_second.status == "candidate"
        assert stored_third is not None and stored_third.status == "active"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_memory_invalidation_expiry_and_tombstone_leave_no_recall_path() -> None:
    database, engine = _database()
    try:
        scope, generated = await _seed_candidates(database, "terminal-memory")
        first = _semantic(generated)
        published = await _evaluate_approve_publish(database, scope, first.id)
        lifecycle = MemoryLifecycleService(database)

        with pytest.raises(DomainError) as not_due:
            await lifecycle.expire(
                _context(scope, "operator:publisher"),
                first.id,
                reason="No expiry is configured.",
                expected_revision=published.revision,
            )
        assert not_due.value.code == "MEMORY_NOT_DUE_FOR_EXPIRY"

        invalidated = await lifecycle.invalidate(
            _context(scope, "operator:publisher"),
            first.id,
            reason="Source knowledge was revoked.",
            expected_revision=published.revision,
        )
        deleted = await lifecycle.delete(
            _context(scope, "operator:publisher"),
            first.id,
            reason="Retain only a tombstone and audit trail.",
            expected_revision=invalidated.revision,
        )
        assert invalidated.status == "invalidated"
        assert deleted.status == "deleted"

        results = await MemoryRetriever(database).search(
            _context(scope, "runtime:reader"),
            MemoryQueryContext(project_id=scope["project_id"]),
            "candidate governance independent review",
        )
        assert first.id not in {item.memory_id for item in results}

        async with database.tenant_transaction(_context(scope, "reader")) as session:
            stored = await session.get(Memory, first.id)
            source_count = await session.scalar(
                select(func.count(GrowthSource.id)).where(GrowthSource.memory_id == first.id)
            )
            chunk_count = await session.scalar(
                select(func.count(MemoryChunk.id)).where(MemoryChunk.memory_id == first.id)
            )
        assert stored is not None and stored.deleted_at is not None
        assert source_count >= 1
        assert chunk_count >= 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_due_active_memory_transitions_to_expired_and_is_not_recalled() -> None:
    database, engine = _database()
    try:
        scope, generated = await _seed_candidates(database, "expiry")
        first = _semantic(generated)
        published = await _evaluate_approve_publish(database, scope, first.id)
        lifecycle = MemoryLifecycleService(database)
        expires_at = datetime.now(UTC) + timedelta(seconds=2)
        revised = await lifecycle.revise(
            _context(scope, "operator:editor"),
            first.id,
            content="Temporary governed knowledge expires after its declared review window.",
            reason="Create a bounded-lived semantic revision.",
            expected_revision=published.revision,
            expires_at=expires_at,
        )
        active = await _evaluate_approve_publish(
            database,
            scope,
            revised.id,
            memory_revision=revised.revision,
        )
        delay = max(0.0, (expires_at - datetime.now(UTC)).total_seconds()) + 0.05
        await asyncio.sleep(delay)

        expired = await lifecycle.expire(
            _context(scope, "operator:publisher"),
            revised.id,
            reason="The declared review window elapsed.",
            expected_revision=active.revision,
        )
        assert expired.status == "expired"

        results = await MemoryRetriever(database).search(
            _context(scope, "runtime:reader"),
            MemoryQueryContext(project_id=scope["project_id"]),
            "temporary governed knowledge review window",
        )
        assert revised.id not in {item.memory_id for item in results}
    finally:
        await engine.dispose()


class _FailingIndexer:
    async def index_memory_in_session(self, session, memory):
        raise RuntimeError("injected indexing failure")


@pytest.mark.asyncio
async def test_publication_rolls_back_state_and_events_when_indexing_fails() -> None:
    database, engine = _database()
    try:
        scope, generated = await _seed_candidates(database, "atomic-index")
        memory = _semantic(generated)
        await GrowthEvaluationService(database).evaluate(
            _context(scope, "evaluator:caller"), "memory", memory.id
        )
        request = await GrowthApprovalService(database).request(
            _context(scope, "operator:requester"),
            "memory",
            memory.id,
            expected_revision=1,
        )
        await GrowthApprovalService(database).decide(
            _context(scope, "operator:reviewer"),
            request.id,
            decision="approved",
            reason="The candidate passed independent review.",
            expected_revision=request.revision,
        )

        lifecycle = MemoryLifecycleService(database, indexer=_FailingIndexer())
        with pytest.raises(RuntimeError, match="injected indexing failure"):
            await lifecycle.publish(
                _context(scope, "operator:publisher"),
                memory.id,
                expected_revision=1,
            )

        async with database.tenant_transaction(_context(scope, "reader")) as session:
            stored = await session.get(Memory, memory.id)
            chunk_count = await session.scalar(
                select(func.count(MemoryChunk.id)).where(MemoryChunk.memory_id == memory.id)
            )
            published_events = await session.scalar(
                select(func.count(Event.id)).where(
                    Event.aggregate_id == memory.id,
                    Event.event_type == "MemoryPublished",
                )
            )
        assert stored is not None
        assert stored.status == "candidate"
        assert stored.revision == 1
        assert chunk_count == 0
        assert published_events == 0
    finally:
        await engine.dispose()
