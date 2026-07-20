"""Deterministic, bounded Conversation context selection for Runtime Runs."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nico_agent.domain.models import Conversation, ConversationTurn, Run


async def select_conversation_context(
    session: AsyncSession,
    run: Run,
    *,
    max_chars: int,
) -> dict[str, Any] | None:
    current = await session.scalar(
        select(ConversationTurn).where(
            ConversationTurn.tenant_id == run.tenant_id,
            ConversationTurn.run_id == run.id,
        )
    )
    if current is None:
        return None
    conversation = await session.scalar(
        select(Conversation).where(
            Conversation.tenant_id == run.tenant_id,
            Conversation.id == current.conversation_id,
        )
    )
    if conversation is None:
        return None

    bounded_chars = min(max(max_chars, 4_000), 256_000)
    history_budget = max(1_000, int(bounded_chars * 0.55))
    previous = list(
        await session.scalars(
            select(ConversationTurn)
            .where(
                ConversationTurn.tenant_id == run.tenant_id,
                ConversationTurn.conversation_id == conversation.id,
                ConversationTurn.sequence < current.sequence,
                ConversationTurn.sequence > conversation.summary_through_sequence,
                ConversationTurn.status == "completed",
            )
            .order_by(ConversationTurn.sequence.desc())
        )
    )
    selected, omitted = _select_recent_turns(previous, history_budget)

    raw_artifact_refs: list[dict[str, Any]] = []
    for turn in (*selected, current):
        for reference in turn.artifact_refs:
            if isinstance(reference, dict):
                raw_artifact_refs.append(reference)
    raw_artifact_refs = list(
        {str(item.get("artifact_id")): item for item in raw_artifact_refs}.values()
    )
    artifact_budget = max(500, int(bounded_chars * 0.15))
    artifact_refs: list[dict[str, Any]] = []
    artifact_chars = 0
    for reference in raw_artifact_refs:
        bounded = {
            key: reference.get(key)
            for key in (
                "artifact_id",
                "owner_run_id",
                "name",
                "content_type",
                "sha256",
                "size_bytes",
            )
        }
        summary = reference.get("summary")
        if isinstance(summary, str):
            remaining = max(0, artifact_budget - artifact_chars)
            bounded["summary"] = summary[:remaining]
        size = len(json.dumps(bounded, ensure_ascii=False, default=str))
        if artifact_refs and artifact_chars + size > artifact_budget:
            break
        artifact_refs.append(bounded)
        artifact_chars += size

    contexts: list[dict[str, Any]] = []
    source_refs: list[str] = []
    summary_hash = None
    if conversation.summary:
        summary_hash = hashlib.sha256(conversation.summary.encode()).hexdigest()
        summary_budget = max(1_000, int(bounded_chars * 0.25))
        bounded_summary = conversation.summary[:summary_budget]
        contexts.append(
            {
                "source": f"conversation:{conversation.id}:summary",
                "kind": "conversation_summary",
                "trust": "untrusted_data",
                "through_sequence": conversation.summary_through_sequence,
                "content_hash": summary_hash,
                "content": bounded_summary,
                "content_truncated": len(bounded_summary) < len(conversation.summary),
            }
        )
        source_refs.append(f"conversation-summary:{conversation.id}:{summary_hash}")
    if selected:
        contexts.append(
            {
                "source": f"conversation:{conversation.id}:turns",
                "kind": "recent_conversation_turns",
                "trust": "untrusted_data",
                "content": [_turn_payload(turn) for turn in selected],
            }
        )
        source_refs.extend(f"conversation-turn:{turn.id}" for turn in selected)
    if artifact_refs:
        contexts.append(
            {
                "source": f"conversation:{conversation.id}:artifacts",
                "kind": "artifact_references",
                "trust": "untrusted_data",
                "content": artifact_refs,
                "note": "References and bounded metadata only; artifact bodies are not injected.",
            }
        )
        source_refs.extend(
            f"artifact:{item.get('artifact_id')}"
            for item in artifact_refs
            if item.get("artifact_id")
        )
    selected_ids = [str(turn.id) for turn in selected] + [str(current.id)]
    return {
        "schema_version": 1,
        "conversation_id": str(conversation.id),
        "conversation_turn_id": str(current.id),
        "selected_turn_ids": selected_ids,
        "conversation_summary_hash": summary_hash,
        "artifact_refs": artifact_refs,
        "token_budget": run.token_budget,
        "source_refs": source_refs,
        "untrusted_context": contexts,
        "truncation": {
            "strategy": "summary_then_newest_complete_turns",
            "max_chars": bounded_chars,
            "history_budget_chars": history_budget,
            "selected_previous_turns": len(selected),
            "omitted_previous_turns": omitted,
            "selected_artifacts": len(artifact_refs),
            "omitted_artifacts": len(raw_artifact_refs) - len(artifact_refs),
            "summary_through_sequence": conversation.summary_through_sequence,
        },
    }


def _turn_payload(turn: ConversationTurn) -> dict[str, Any]:
    return {
        "turn_id": str(turn.id),
        "sequence": turn.sequence,
        "user": turn.user_input,
        "assistant": turn.assistant_output,
        "artifact_refs": turn.artifact_refs,
    }


def _select_recent_turns(
    newest_first: list[ConversationTurn], history_budget: int
) -> tuple[list[ConversationTurn], int]:
    """Select one contiguous newest-first suffix, returned in chronological order."""

    selected_newest: list[ConversationTurn] = []
    consumed = 0
    omitted = 0
    for index, turn in enumerate(newest_first):
        payload = _turn_payload(turn)
        size = len(json.dumps(payload, ensure_ascii=False, default=str))
        if selected_newest and consumed + size > history_budget:
            omitted += len(newest_first) - index
            break
        if not selected_newest and size > history_budget:
            payload["user"] = turn.user_input[: history_budget // 2]
            payload["assistant"] = _truncate_value(turn.assistant_output, history_budget // 2)
            size = len(json.dumps(payload, ensure_ascii=False, default=str))
        consumed += size
        selected_newest.append(turn)
    return list(reversed(selected_newest)), omitted


def _truncate_value(value: Any, max_chars: int) -> Any:
    rendered = json.dumps(value, ensure_ascii=False, default=str)
    if len(rendered) <= max_chars:
        return value
    return rendered[:max_chars] + "…[TRUNCATED]"
