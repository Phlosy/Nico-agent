from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import create_async_engine

from nico_agent.config import Settings
from nico_agent.database import Database
from nico_agent.domain.models import (
    Agent,
    AgentVersion,
    Conversation,
    ConversationTurn,
    Project,
    Run,
    Task,
    Tenant,
)

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with the Compose dependencies running",
)


async def seed_pending_run(
    database: Database,
    *,
    priority: int = 0,
    run_config: dict | None = None,
) -> Run:
    suffix = uuid4().hex[:10]
    async with database.admin_transaction() as session:
        tenant = Tenant(name=f"Lease {suffix}", slug=f"lease-{suffix}")
        session.add(tenant)
        await session.flush()
        project = Project(tenant_id=tenant.id, name=f"project-{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            name=f"agent-{suffix}",
            display_name="Lease Agent",
        )
        session.add_all([project, agent])
        await session.flush()
        version = AgentVersion(
            tenant_id=tenant.id,
            agent_id=agent.id,
            version=1,
            status="published",
            role="worker-test",
            mandate="Prove lease behavior",
            run_config=run_config or {},
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
            title="Lease acceptance",
            status="running",
            priority=priority,
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
        run_id = run.id
        tenant_id = tenant.id

    async with database.admin_transaction() as session:
        return (
            (
                await session.execute(
                    text("SELECT * FROM runs WHERE id = :run_id AND tenant_id = :tenant_id"),
                    {"run_id": run_id, "tenant_id": tenant_id},
                )
            )
            .mappings()
            .one()
        )


async def seed_conversation_queue(database: Database, *, length: int = 3) -> dict:
    suffix = uuid4().hex[:10]
    async with database.admin_transaction() as session:
        tenant = Tenant(name=f"Queue {suffix}", slug=f"queue-{suffix}")
        session.add(tenant)
        await session.flush()
        project = Project(tenant_id=tenant.id, name=f"project-{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            name=f"agent-{suffix}",
            display_name="Queue Agent",
        )
        session.add_all([project, agent])
        await session.flush()
        version = AgentVersion(
            tenant_id=tenant.id,
            agent_id=agent.id,
            version=1,
            status="published",
            role="queue-test",
            mandate="Prove serialized conversation execution",
            run_config={},
            content_hash="b" * 64,
        )
        session.add(version)
        await session.flush()
        agent.current_version_id = version.id
        agent.status = "ready"
        conversation = Conversation(
            tenant_id=tenant.id,
            project_id=project.id,
            agent_id=agent.id,
            agent_version_id=version.id,
            title=f"Queue {suffix}",
            created_by="queue-test",
            idempotency_key=f"queue:{suffix}",
        )
        session.add(conversation)
        await session.flush()

        turns: list[ConversationTurn] = []
        runs: list[Run] = []
        for sequence in range(1, length + 1):
            task = Task(
                tenant_id=tenant.id,
                project_id=project.id,
                assignee_agent_id=agent.id,
                title=f"Queue turn {sequence}",
                status="running",
                priority=2_147_483_640,
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
            turn = ConversationTurn(
                tenant_id=tenant.id,
                conversation_id=conversation.id,
                sequence=sequence,
                user_input=f"turn {sequence}",
                task_id=task.id,
                run_id=run.id,
                status="queued",
                idempotency_key=f"queue:{suffix}:{sequence}",
            )
            session.add(turn)
            await session.flush()
            turns.append(turn)
            runs.append(run)

        conversation.last_turn_id = turns[-1].id
        tenant_id = tenant.id
        conversation_id = conversation.id
        turn_ids = [turn.id for turn in turns]
        run_ids = [run.id for run in runs]

    return {
        "tenant_id": tenant_id,
        "conversation_id": conversation_id,
        "turn_ids": turn_ids,
        "run_ids": run_ids,
    }


@pytest.mark.asyncio
async def test_claimer_role_is_minimal_and_runtime_session_has_force_rls() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    try:
        async with engine.connect() as connection:
            role = (
                await connection.execute(
                    text(
                        "SELECT rolsuper, rolbypassrls FROM pg_roles "
                        "WHERE rolname = 'nico_worker_claimer'"
                    )
                )
            ).one()
            protected = (
                await connection.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname = 'runtime_sessions'"
                    )
                )
            ).one()
            function_security = (
                await connection.execute(
                    text(
                        "SELECT prosecdef, proconfig FROM pg_proc WHERE proname = 'claim_next_run'"
                    )
                )
            ).one()
        assert role == (False, False)
        assert protected == (True, True)
        assert function_security[0] is True
        assert "search_path=pg_catalog, public" in function_security[1]

        with pytest.raises(ProgrammingError):
            async with engine.begin() as connection:
                await connection.execute(text("SET LOCAL ROLE nico_worker_claimer"))
                await connection.execute(text("SELECT * FROM runs"))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_claim_is_unique_and_expired_lease_is_reclaimed() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        seeded = await seed_pending_run(database, priority=2_000_000_000)
        claims = await asyncio.gather(
            database.claim_next_run("worker-a", 30),
            database.claim_next_run("worker-b", 30),
        )
        target_claims = [
            claim for claim in claims if claim is not None and claim.run_id == seeded["id"]
        ]
        winner = target_claims[0]

        assert len(target_claims) == 1
        assert winner.run_id == seeded["id"]
        assert winner.tenant_id == seeded["tenant_id"]
        assert winner.previous_status == "pending"

        async with database.admin_transaction() as session:
            await session.execute(
                text(
                    "UPDATE runs SET lease_expires_at = :expired "
                    "WHERE id = :run_id AND tenant_id = :tenant_id"
                ),
                {
                    "expired": datetime.now(UTC) - timedelta(seconds=1),
                    "run_id": winner.run_id,
                    "tenant_id": winner.tenant_id,
                },
            )

        reclaimed = await database.claim_next_run("worker-c", 30)
        assert reclaimed is not None
        assert reclaimed.run_id == winner.run_id
        assert reclaimed.lease_token != winner.lease_token
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_claim_orders_higher_priority_first() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        low = await seed_pending_run(database, priority=-10)
        high = await seed_pending_run(database, priority=50)

        claimed = await database.claim_next_run("priority-worker", 30)

        assert claimed is not None
        assert claimed.run_id == high["id"]
        assert claimed.run_id != low["id"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_conversation_queue_claims_only_the_head_in_sequence() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        seeded = await seed_conversation_queue(database)
        claims = await asyncio.gather(
            database.claim_next_run("queue-worker-a", 30),
            database.claim_next_run("queue-worker-b", 30),
        )

        target_claims = [
            claim
            for claim in claims
            if claim is not None and claim.run_id in set(seeded["run_ids"])
        ]
        assert [claim.run_id for claim in target_claims] == [seeded["run_ids"][0]]

        async with database.admin_transaction() as session:
            await session.execute(
                text(
                    "UPDATE runs SET status = 'completed', result = '{}'::jsonb, "
                    "revision = revision + 1 WHERE id = :run_id"
                ),
                {"run_id": seeded["run_ids"][0]},
            )

        next_claim = await database.claim_next_run("queue-worker-c", 30)
        assert next_claim is not None
        assert next_claim.run_id == seeded["run_ids"][1]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_abnormal_head_pauses_queue_but_future_cancel_does_not() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        seeded = await seed_conversation_queue(database)
        async with database.admin_transaction() as session:
            await session.execute(
                text(
                    "UPDATE runs SET status = 'cancelled', revision = revision + 1 "
                    "WHERE id = :run_id"
                ),
                {"run_id": seeded["run_ids"][2]},
            )
            state = (
                await session.execute(
                    text(
                        "SELECT queue_state, queue_pause_turn_id FROM conversations "
                        "WHERE id = :conversation_id"
                    ),
                    {"conversation_id": seeded["conversation_id"]},
                )
            ).one()
            assert state == ("active", None)

            await session.execute(
                text(
                    "UPDATE runs SET status = 'failed', "
                    "error = '{\"code\": \"TEST_FAILURE\"}'::jsonb, "
                    "revision = revision + 1 WHERE id = :run_id"
                ),
                {"run_id": seeded["run_ids"][0]},
            )
            state = (
                await session.execute(
                    text(
                        "SELECT queue_state, queue_pause_reason, queue_pause_turn_id "
                        "FROM conversations WHERE id = :conversation_id"
                    ),
                    {"conversation_id": seeded["conversation_id"]},
                )
            ).one()
            assert state == ("paused", "run_failed", seeded["turn_ids"][0])

        claim = await database.claim_next_run("paused-queue-worker", 30)
        assert claim is None or claim.run_id not in set(seeded["run_ids"][1:])
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_paused_queue_allows_started_head_lease_recovery() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        seeded = await seed_conversation_queue(database, length=2)
        first = await database.claim_next_run("recovery-worker-a", 30)
        assert first is not None
        assert first.run_id == seeded["run_ids"][0]

        expired = datetime.now(UTC) - timedelta(seconds=1)
        async with database.admin_transaction() as session:
            await session.execute(
                text(
                    "UPDATE runs SET status = 'running', lease_expires_at = :expired, "
                    "revision = revision + 1 WHERE id = :run_id"
                ),
                {"expired": expired, "run_id": seeded["run_ids"][0]},
            )
            await session.execute(
                text(
                    "UPDATE conversations SET queue_state = 'paused', "
                    "queue_pause_reason = 'tool_rejected', "
                    "queue_pause_turn_id = :turn_id, queue_paused_at = now(), "
                    "revision = revision + 1 WHERE id = :conversation_id"
                ),
                {
                    "turn_id": seeded["turn_ids"][0],
                    "conversation_id": seeded["conversation_id"],
                },
            )

        recovered = await database.claim_next_run("recovery-worker-b", 30)
        assert recovered is not None
        assert recovered.run_id == seeded["run_ids"][0]
        assert recovered.lease_token != first.lease_token
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_paused_queue_allows_only_pause_cause_retry() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        seeded = await seed_conversation_queue(database, length=2)
        async with database.admin_transaction() as session:
            await session.execute(
                text(
                    "UPDATE runs SET status = 'failed', revision = revision + 1 "
                    "WHERE id = :run_id"
                ),
                {"run_id": seeded["run_ids"][0]},
            )
            retry_id = (
                await session.execute(
                    text(
                        "INSERT INTO runs "
                        "(tenant_id, task_id, agent_id, agent_version_id, retry_of_run_id, "
                        "attempt, status) "
                        "SELECT tenant_id, task_id, agent_id, agent_version_id, id, 2, 'pending' "
                        "FROM runs WHERE id = :run_id RETURNING id"
                    ),
                    {"run_id": seeded["run_ids"][0]},
                )
            ).scalar_one()
            await session.execute(
                text(
                    "UPDATE conversation_turns SET run_id = :retry_id, status = 'queued', "
                    "revision = revision + 1 WHERE id = :turn_id"
                ),
                {"retry_id": retry_id, "turn_id": seeded["turn_ids"][0]},
            )

        claim = await database.claim_next_run("retry-worker", 30)
        assert claim is not None
        assert claim.run_id == retry_id

        other = await database.claim_next_run("retry-worker-other", 30)
        assert other is None or other.run_id != seeded["run_ids"][1]
    finally:
        await engine.dispose()
