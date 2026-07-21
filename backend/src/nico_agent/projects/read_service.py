"""Read-only ProjectSession projections built from authoritative execution events."""

from __future__ import annotations

import json
from itertools import islice
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select

from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import ResourceNotFound
from nico_agent.domain.models import (
    Conversation,
    ConversationTurn,
    Event,
    Project,
    ProjectMember,
    ProjectSession,
    Run,
    Task,
)
from nico_agent.projects.contracts import ProjectTimelineEntryRead, ProjectTimelinePage

_PRIVATE_KEYS = {
    "api_key",
    "arguments",
    "authorization",
    "chain_of_thought",
    "completion",
    "credential",
    "object_key",
    "prompt",
    "raw_reasoning",
    "secret",
    "trajectory",
}
_MAX_DEPTH = 4
_MAX_ITEMS = 50
_MAX_STRING = 2_000
_MAX_PAYLOAD_BYTES = 16_000


class ProjectSessionReadService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def timeline(
        self,
        context: TenantContext,
        project_id: UUID,
        session_id: UUID,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> ProjectTimelinePage:
        async with self.database.tenant_transaction(context) as session:
            project_session = await session.scalar(
                select(ProjectSession)
                .join(
                    Project,
                    (Project.tenant_id == ProjectSession.tenant_id)
                    & (Project.id == ProjectSession.project_id),
                )
                .join(
                    ProjectMember,
                    (ProjectMember.tenant_id == ProjectSession.tenant_id)
                    & (ProjectMember.id == ProjectSession.project_member_id),
                )
                .where(
                    ProjectSession.tenant_id == context.tenant_id,
                    ProjectSession.project_id == project_id,
                    ProjectSession.id == session_id,
                    Project.kind == "shared",
                )
            )
            if project_session is None:
                raise ResourceNotFound("project_session", str(session_id))

            conversation_ids = select(Conversation.id).where(
                Conversation.tenant_id == context.tenant_id,
                Conversation.project_id == project_id,
                Conversation.project_session_id == session_id,
            )
            turn_ids = (
                select(ConversationTurn.id)
                .join(
                    Conversation,
                    (Conversation.tenant_id == ConversationTurn.tenant_id)
                    & (Conversation.id == ConversationTurn.conversation_id),
                )
                .where(
                    ConversationTurn.tenant_id == context.tenant_id,
                    Conversation.project_id == project_id,
                    Conversation.project_session_id == session_id,
                )
            )
            task_ids = select(Task.id).where(
                Task.tenant_id == context.tenant_id,
                Task.project_id == project_id,
                Task.project_session_id == session_id,
            )
            run_ids = (
                select(Run.id)
                .join(
                    Task,
                    (Task.tenant_id == Run.tenant_id) & (Task.id == Run.task_id),
                )
                .where(
                    Run.tenant_id == context.tenant_id,
                    Task.project_id == project_id,
                    Task.project_session_id == session_id,
                )
            )
            related = or_(
                and_(Event.aggregate_type == "project", Event.aggregate_id == project_id),
                and_(
                    Event.aggregate_type == "project_member",
                    Event.aggregate_id == project_session.project_member_id,
                ),
                and_(
                    Event.aggregate_type == "project_session",
                    Event.aggregate_id == session_id,
                ),
                and_(
                    Event.aggregate_type == "conversation",
                    Event.aggregate_id.in_(conversation_ids),
                ),
                and_(
                    Event.aggregate_type == "conversation_turn",
                    Event.aggregate_id.in_(turn_ids),
                ),
                and_(Event.aggregate_type == "task", Event.aggregate_id.in_(task_ids)),
                Event.run_id.in_(run_ids),
            )
            events = list(
                await session.scalars(
                    select(Event)
                    .where(
                        Event.tenant_id == context.tenant_id,
                        Event.sequence > after_sequence,
                        related,
                    )
                    .order_by(Event.sequence)
                    .limit(limit + 1)
                )
            )

        has_more = len(events) > limit
        visible = events[:limit]
        entries = [
            ProjectTimelineEntryRead(
                sequence=event.sequence,
                occurred_at=event.created_at,
                kind=self.entry_kind(event.event_type, event.aggregate_type),
                event_type=event.event_type,
                resource_type=event.aggregate_type,
                resource_id=event.aggregate_id,
                run_id=event.run_id,
                actor_id=event.actor_id,
                facts=self.sanitize_payload(event.payload),
                links=self.links(
                    project_id=project_id,
                    session_id=session_id,
                    aggregate_type=event.aggregate_type,
                    aggregate_id=event.aggregate_id,
                    run_id=event.run_id,
                ),
            )
            for event in visible
        ]
        return ProjectTimelinePage(
            entries=entries,
            next_cursor=entries[-1].sequence if has_more and entries else None,
            has_more=has_more,
        )

    @staticmethod
    def entry_kind(event_type: str, aggregate_type: str) -> str:
        marker = f"{event_type} {aggregate_type}".lower()
        if "artifact" in marker:
            return "artifact"
        if "tool" in marker:
            return "tool"
        if "plan" in marker:
            return "plan"
        if "delegat" in marker or "child" in marker:
            return "delegation"
        if "conversation" in marker or "turn" in marker:
            return "conversation"
        if aggregate_type == "task" or event_type.lower().startswith("task"):
            return "task"
        if aggregate_type in {"run", "runtime_session"} or event_type.lower().startswith("run"):
            return "run"
        return "state"

    @classmethod
    def sanitize_payload(cls, value: Any, *, _depth: int = 0) -> Any:
        sanitized = cls._sanitize_value(value, depth=_depth)
        if _depth == 0:
            encoded = json.dumps(sanitized, ensure_ascii=False, default=str).encode()
            if len(encoded) > _MAX_PAYLOAD_BYTES:
                if not isinstance(sanitized, dict):
                    return "[payload omitted: exceeds timeline bound]"
                bounded: dict[str, Any] = {"_truncated": True}
                for key, item in sanitized.items():
                    candidate = {**bounded, key: item}
                    if len(json.dumps(candidate, ensure_ascii=False).encode()) > _MAX_PAYLOAD_BYTES:
                        continue
                    bounded[key] = item
                return bounded
        return sanitized

    @classmethod
    def _sanitize_value(cls, value: Any, *, depth: int) -> Any:
        if depth >= _MAX_DEPTH:
            return "[bounded]"
        if isinstance(value, dict):
            return {
                str(key): cls._sanitize_value(item, depth=depth + 1)
                for key, item in islice(value.items(), _MAX_ITEMS)
                if str(key).lower() not in _PRIVATE_KEYS
            }
        if isinstance(value, list):
            return [cls._sanitize_value(item, depth=depth + 1) for item in value[:_MAX_ITEMS]]
        if isinstance(value, str) and len(value) > _MAX_STRING:
            return f"{value[:_MAX_STRING]}…"
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return str(value)[:_MAX_STRING]

    @staticmethod
    def links(
        *,
        project_id: UUID,
        session_id: UUID,
        aggregate_type: str,
        aggregate_id: UUID,
        run_id: UUID | None,
    ) -> dict[str, str]:
        links = {
            "session": f"/api/v1/projects/{project_id}/sessions/{session_id}",
        }
        if run_id is not None:
            links["run"] = f"/api/v1/runs/{run_id}"
        if aggregate_type == "conversation":
            links["resource"] = f"/api/v1/conversations/{aggregate_id}"
        elif aggregate_type == "conversation_turn":
            links["resource"] = f"/api/v1/conversation-turns/{aggregate_id}"
        elif aggregate_type == "task":
            links["resource"] = f"/api/v1/tasks/{aggregate_id}"
        elif aggregate_type == "run":
            links["resource"] = f"/api/v1/runs/{aggregate_id}"
        elif aggregate_type == "artifact" and run_id is not None:
            links["resource"] = f"/api/v1/runs/{run_id}/artifacts"
            links["content"] = f"/api/v1/runs/{run_id}/artifacts/{aggregate_id}/content"
        elif aggregate_type == "tool_call" and run_id is not None:
            links["resource"] = f"/api/v1/runs/{run_id}/tool-calls"
        elif aggregate_type == "plan" and run_id is not None:
            links["resource"] = f"/api/v1/runs/{run_id}/plans/{aggregate_id}"
        elif aggregate_type == "plan_step" and run_id is not None:
            links["resource"] = f"/api/v1/runs/{run_id}/plans"
        return links
