from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from nico_agent.api import create_app
from nico_agent.config import Settings
from nico_agent.database import Database, TenantContext
from nico_agent.domain.models import (
    Agent,
    AgentVersion,
    ContextSnapshot,
    GrowthSource,
    Memory,
    ModelEndpoint,
    Project,
    Run,
    RunStep,
    RuntimeKnowledgeUsage,
    RuntimeSession,
    Skill,
    SkillVersion,
    Task,
    Tenant,
)
from nico_agent.growth import GrowthApprovalService, GrowthEvaluationService
from nico_agent.growth.contracts import canonical_hash, skill_content_hash
from nico_agent.growth.snapshot import TrajectorySnapshotBuilder
from nico_agent.memory.chunking import content_hash
from nico_agent.memory.lifecycle import MemoryLifecycleService
from nico_agent.models.contracts import (
    ModelCapability,
    ModelStreamEvent,
    ModelStreamEventType,
    ModelUsage,
)
from nico_agent.models.gateway import ModelGateway
from nico_agent.models.registry import ModelProviderRegistry
from nico_agent.runtime import NicoNativeRuntimeProvider, RuntimeProviderRegistry
from nico_agent.runtime.executor import RuntimeWorker
from nico_agent.skills import SkillLifecycleService

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with PostgreSQL running",
)

MEMORY_MARKER = "GOAL_K_APPROVED_MEMORY"
SKILL_MARKER = "GOAL_K_APPROVED_SKILL"
CANDIDATE_MARKER = "GOAL_K_CANDIDATE_MEMORY"
DRAFT_MARKER = "GOAL_K_DRAFT_SKILL"


class KnowledgeAwareModelProvider:
    name = "openai_compatible"

    def __init__(self) -> None:
        self.rendered = ""

    def describe_capabilities(self):
        return frozenset({ModelCapability.STREAMING})

    async def stream(self, request):
        self.rendered = "\n".join(message.content or "" for message in request.messages)
        assert MEMORY_MARKER in self.rendered
        assert SKILL_MARKER in self.rendered
        assert CANDIDATE_MARKER not in self.rendered
        assert DRAFT_MARKER not in self.rendered
        yield ModelStreamEvent(type=ModelStreamEventType.RESPONSE_STARTED)
        yield ModelStreamEvent(
            type=ModelStreamEventType.TEXT_DELTA,
            text_delta="Goal K used frozen published knowledge.",
        )
        yield ModelStreamEvent(
            type=ModelStreamEventType.RESPONSE_COMPLETED,
            finish_reason="stop",
            usage=ModelUsage(
                input_tokens=40,
                output_tokens=7,
                total_tokens=47,
                status="exact",
            ),
            provider_request_id="goal-k-integration",
        )


def _skill_version(*, tenant_id, skill_id, version: int, marker: str):
    row = SkillVersion(
        tenant_id=tenant_id,
        skill_id=skill_id,
        version=version,
        status="draft",
        conditions={"marker": marker},
        preconditions=[],
        input_schema={"type": "object"},
        steps=[{"id": "apply-approved-knowledge", "instruction": f"Apply {marker}"}],
        tools=[],
        output_schema={"type": "object"},
        validation={"required": ["content"]},
        failure_modes=[{"code": "KNOWLEDGE_UNAVAILABLE"}],
        content_hash="0" * 64,
        created_by="goal-k-fixture",
    )
    row.content_hash = skill_content_hash(row)
    return row


def _source_snapshot(source_hash: str) -> tuple[str, dict]:
    trajectory = {
        "run_status": "completed",
        "steps": [{"status": "completed", "tool_calls": []}],
        "runtime": None,
    }
    trajectory_hash = canonical_hash(trajectory)
    trajectory["snapshot_hash"] = trajectory_hash
    return trajectory_hash, {
        "candidate_key": source_hash,
        "trajectory": trajectory,
    }


@pytest.mark.asyncio
async def test_runtime_consumes_only_published_frozen_knowledge_and_tracks_effects() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        suffix = uuid4().hex[:10]
        async with database.admin_transaction() as session:
            tenant = Tenant(
                name=f"Knowledge {suffix}",
                slug=f"knowledge-{suffix}",
                settings={
                    "memory_policy": {
                        "enabled": True,
                        "scopes": ["project"],
                        "memory_types": ["semantic"],
                        "top_k": 5,
                        "max_chars": 4_000,
                        "max_tokens": 128,
                        "minimum_similarity": 0,
                    },
                    "skill_policy": {
                        "enabled": True,
                        "scopes": ["project"],
                        "allowed_skill_ids": [],
                        "top_k": 5,
                        "max_chars": 4_000,
                        "max_tokens": 256,
                    },
                },
            )
            session.add(tenant)
            await session.flush()
            project = Project(tenant_id=tenant.id, name=f"project-{suffix}")
            agent = Agent(
                tenant_id=tenant.id,
                name=f"agent-{suffix}",
                display_name="Knowledge Agent",
            )
            endpoint = ModelEndpoint(
                tenant_id=tenant.id,
                stable_key="goal-k",
                revision=1,
                display_name="Goal K fake model",
                base_url="https://models.example/v1",
                credential_ref="env:NICO_MODEL_SECRET_TEST",
                allowed_models=["goal-k-model"],
                capabilities={"streaming": True},
            )
            session.add_all([project, agent, endpoint])
            await session.flush()

            approved_skill = Skill(
                tenant_id=tenant.id,
                name=f"approved-{suffix}",
                description="Published knowledge procedure",
                scope_type="project",
                project_id=project.id,
                status="candidate",
                created_by="goal-k-fixture",
            )
            draft_skill = Skill(
                tenant_id=tenant.id,
                name=f"draft-{suffix}",
                description="Unpublished procedure",
                scope_type="project",
                project_id=project.id,
                status="candidate",
                created_by="goal-k-fixture",
            )
            session.add_all([approved_skill, draft_skill])
            await session.flush()
            published_version = _skill_version(
                tenant_id=tenant.id,
                skill_id=approved_skill.id,
                version=1,
                marker=SKILL_MARKER,
            )
            draft_version = _skill_version(
                tenant_id=tenant.id,
                skill_id=draft_skill.id,
                version=1,
                marker=DRAFT_MARKER,
            )
            session.add_all([published_version, draft_version])
            await session.flush()

            tenant.settings = {
                **tenant.settings,
                "skill_policy": {
                    **tenant.settings["skill_policy"],
                    "allowed_skill_ids": [str(approved_skill.id), str(draft_skill.id)],
                },
            }

            version = AgentVersion(
                tenant_id=tenant.id,
                agent_id=agent.id,
                version=1,
                status="published",
                role="analyst",
                mandate="Use approved project knowledge",
                runtime_provider="nico_native",
                execution_mode="direct",
                model_endpoint_id=endpoint.id,
                model_name="goal-k-model",
                memory_policy={
                    "enabled": True,
                    "scopes": ["project", "agent"],
                    "memory_types": ["semantic"],
                    "top_k": 2,
                    "max_chars": 2_000,
                    "max_tokens": 12,
                    "minimum_similarity": 0,
                },
                skill_policy={
                    "enabled": True,
                    "scopes": ["project", "agent"],
                    "allowed_skill_ids": [str(approved_skill.id), str(draft_skill.id)],
                    "top_k": 2,
                    "max_chars": 2_000,
                    "max_tokens": 128,
                },
                content_hash="a" * 64,
            )
            session.add(version)
            await session.flush()
            agent.current_version_id = version.id
            agent.status = "ready"

            source_task = Task(
                tenant_id=tenant.id,
                project_id=project.id,
                assignee_agent_id=agent.id,
                title="Produce governed Goal K knowledge",
                status="completed",
            )
            session.add(source_task)
            await session.flush()
            source_run = Run(
                tenant_id=tenant.id,
                task_id=source_task.id,
                agent_id=agent.id,
                agent_version_id=version.id,
                attempt=1,
                status="completed",
                result={"content": "reviewed knowledge source"},
                ended_at=datetime.now(UTC),
            )
            session.add(source_run)
            await session.flush()
            source_step = RunStep(
                tenant_id=tenant.id,
                run_id=source_run.id,
                sequence=1,
                kind="result",
                status="completed",
                output={"content": "reviewed knowledge source"},
                ended_at=datetime.now(UTC),
            )
            session.add(source_step)
            await session.flush()

            approved_memory_content = f"{MEMORY_MARKER}: volatility convention for this project."
            approved_memory = Memory(
                tenant_id=tenant.id,
                version=1,
                memory_type="semantic",
                scope_type="project",
                project_id=project.id,
                status="candidate",
                content=approved_memory_content,
                confidence=0.95,
                content_hash=content_hash(approved_memory_content),
                created_by="goal-k-fixture",
            )
            candidate_memory = Memory(
                tenant_id=tenant.id,
                version=1,
                memory_type="semantic",
                scope_type="project",
                project_id=project.id,
                status="candidate",
                content=f"{CANDIDATE_MARKER}: this must never enter runtime context.",
                confidence=1,
                content_hash=content_hash(
                    f"{CANDIDATE_MARKER}: this must never enter runtime context."
                ),
                created_by="goal-k-fixture",
            )
            session.add_all([approved_memory, candidate_memory])
            await session.flush()
            for subject_type, subject_id, discriminator in (
                ("memory", approved_memory.id, "memory"),
                ("skill_version", published_version.id, "skill"),
            ):
                source_hash = canonical_hash(
                    {"run_id": source_run.id, "subject_id": subject_id, "kind": discriminator}
                )
                trajectory_hash, snapshot = _source_snapshot(source_hash)
                session.add(
                    GrowthSource(
                        tenant_id=tenant.id,
                        subject_type=subject_type,
                        memory_id=subject_id if subject_type == "memory" else None,
                        skill_version_id=(subject_id if subject_type == "skill_version" else None),
                        run_id=source_run.id,
                        run_step_id=source_step.id,
                        agent_version_id=version.id,
                        trajectory_hash=trajectory_hash,
                        generator_name="goal-k-fixture",
                        generator_version="1.0.0",
                        source_hash=source_hash,
                        snapshot=snapshot,
                    )
                )
            await session.flush()

            task = Task(
                tenant_id=tenant.id,
                project_id=project.id,
                assignee_agent_id=agent.id,
                title="Analyze project volatility",
                input={"question": "Use approved volatility knowledge"},
                acceptance={"required": ["content"]},
                status="running",
                priority=2_147_483_647,
            )
            session.add(task)
            await session.flush()
            run = Run(
                tenant_id=tenant.id,
                task_id=task.id,
                agent_id=agent.id,
                agent_version_id=version.id,
                attempt=1,
            )
            session.add(run)
            await session.flush()
            ids = {
                "tenant": tenant.id,
                "run": run.id,
                "memory": approved_memory.id,
                "candidate_memory": candidate_memory.id,
                "skill": approved_skill.id,
                "skill_version": published_version.id,
                "draft_version": draft_version.id,
            }

        def lifecycle_context(actor: str) -> TenantContext:
            return TenantContext(ids["tenant"], actor, uuid4())

        memory_evaluation = await GrowthEvaluationService(database).evaluate(
            lifecycle_context("goal-k-memory-evaluator"), "memory", ids["memory"]
        )
        assert memory_evaluation.verdict == "pass"
        memory_request = await GrowthApprovalService(database).request(
            lifecycle_context("goal-k-memory-requester"),
            "memory",
            ids["memory"],
            expected_revision=1,
        )
        await GrowthApprovalService(database).decide(
            lifecycle_context("goal-k-memory-reviewer"),
            memory_request.id,
            decision="approved",
            reason="Goal K Memory source and content passed independent review.",
            expected_revision=memory_request.revision,
        )
        await MemoryLifecycleService(database).publish(
            lifecycle_context("goal-k-memory-publisher"),
            ids["memory"],
            expected_revision=1,
        )

        skill_evaluation = await GrowthEvaluationService(database).evaluate(
            lifecycle_context("goal-k-skill-evaluator"),
            "skill_version",
            ids["skill_version"],
        )
        assert skill_evaluation.verdict == "pass"
        skill_request = await GrowthApprovalService(database).request(
            lifecycle_context("goal-k-skill-requester"),
            "skill_version",
            ids["skill_version"],
            expected_revision=1,
        )
        await GrowthApprovalService(database).decide(
            lifecycle_context("goal-k-skill-reviewer"),
            skill_request.id,
            decision="approved",
            reason="Goal K Skill source and schemas passed independent review.",
            expected_revision=skill_request.revision,
        )
        await SkillLifecycleService(database).publish_version(
            lifecycle_context("goal-k-skill-publisher"),
            ids["skill"],
            ids["skill_version"],
            expected_skill_revision=1,
            expected_version_revision=1,
        )

        provider = KnowledgeAwareModelProvider()
        worker = RuntimeWorker(
            database,
            RuntimeProviderRegistry(
                [NicoNativeRuntimeProvider(ModelGateway(ModelProviderRegistry([provider])))]
            ),
            worker_id=f"goal-k-{suffix}",
            lease_seconds=10,
            heartbeat_seconds=1,
        )
        assert await worker.execute_once() is True

        async with database.admin_transaction() as session:
            run_row = await session.scalar(select(Run).where(Run.id == ids["run"]))
            runtime = await session.scalar(
                select(RuntimeSession).where(RuntimeSession.run_id == ids["run"])
            )
            context = await session.scalar(
                select(ContextSnapshot).where(ContextSnapshot.run_id == ids["run"])
            )
            usages = list(
                await session.scalars(
                    select(RuntimeKnowledgeUsage)
                    .where(RuntimeKnowledgeUsage.run_id == ids["run"])
                    .order_by(RuntimeKnowledgeUsage.source_type)
                )
            )
            assert run_row is not None and run_row.status == "completed"
            assert runtime is not None
            assert context is not None
            assert len(usages) == 2
            assert {item.status for item in usages} == {"succeeded"}
            assert all(item.context_count == 1 for item in usages)
            assert all(item.model_call_count == 1 for item in usages)
            assert all(item.first_context_snapshot_id == context.id for item in usages)
            assert all(item.first_model_call_id is not None for item in usages)
            assert all(item.effect_metadata["consumed"] is True for item in usages)
            assert {item.memory_id for item in usages if item.memory_id} == {ids["memory"]}
            assert {item.skill_version_id for item in usages if item.skill_version_id} == {
                ids["skill_version"]
            }
            assert str(ids["candidate_memory"]) not in str(runtime.knowledge_selection_snapshot)
            assert str(ids["draft_version"]) not in str(runtime.knowledge_selection_snapshot)
            frozen_selection = runtime.knowledge_selection_snapshot
            assert frozen_selection["memory_token_estimate"] <= 12
            assert frozen_selection["skill_token_estimate"] <= 128
            assert frozen_selection["memory"][0]["content_truncated"] is True
            assert context.effect_metadata["memory_token_estimate"] <= 12
            assert context.effect_metadata["skill_token_estimate"] <= 128

        trajectory = await TrajectorySnapshotBuilder(database).build(
            TenantContext(ids["tenant"], "goal-k-growth", uuid4()), ids["run"]
        )
        assert len(trajectory.knowledge_usages) == 2
        assert {item.status for item in trajectory.knowledge_usages} == {"succeeded"}

        app = create_app(settings=settings, health_service=object(), database=database)
        headers = {"X-Tenant-ID": str(ids["tenant"]), "X-Actor-ID": "goal-k-api"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            contexts_response = await client.get(
                f"/api/v1/runs/{ids['run']}/contexts", headers=headers
            )
            usages_response = await client.get(
                f"/api/v1/runs/{ids['run']}/knowledge-usages", headers=headers
            )
            runtime_response = await client.get(
                f"/api/v1/runs/{ids['run']}/runtime", headers=headers
            )
        assert contexts_response.status_code == 200
        assert usages_response.status_code == 200
        assert runtime_response.status_code == 200
        assert len(contexts_response.json()[0]["memory_refs"]) == 1
        assert len(contexts_response.json()[0]["skill_refs"]) == 1
        assert len(usages_response.json()) == 2
        assert "knowledge_policy_snapshot" in runtime_response.json()
        assert "knowledge_selection_snapshot" not in runtime_response.json()

        # A recovery/rebuild uses the exact frozen selection and never runs live recall.
        async with database.admin_transaction() as session:
            runtime = await session.scalar(
                select(RuntimeSession).where(RuntimeSession.run_id == ids["run"])
            )
            assert runtime is not None
            assert runtime.knowledge_selection_snapshot == frozen_selection
    finally:
        await engine.dispose()
