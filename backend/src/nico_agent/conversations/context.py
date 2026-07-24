"""Deterministic, bounded Conversation context selection for Runtime Runs."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nico_agent.conversations.contracts import ConversationContextMessage
from nico_agent.domain.models import Conversation, ConversationTurn, Run

_TRUNCATION_SUFFIX = "…[TRUNCATED]"


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
    conversation_messages = _project_conversation_messages(selected, history_budget)

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
    if conversation_messages:
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
        "schema_version": 2,
        "conversation_id": str(conversation.id),
        "conversation_turn_id": str(current.id),
        "selected_turn_ids": selected_ids,
        "conversation_messages": [
            message.model_dump(mode="json") for message in conversation_messages
        ],
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


def _select_recent_turns(
    newest_first: list[ConversationTurn], history_budget: int
) -> tuple[list[ConversationTurn], int]:
    """Select one contiguous newest-first suffix, returned in chronological order."""

    selected_newest: list[ConversationTurn] = []
    consumed = 0
    omitted = 0
    for index, turn in enumerate(newest_first):
        raw = _raw_message_payloads([turn])
        size = len(
            json.dumps(
                [_conversation_context_message_payload(item, item["content"]) for item in raw],
                ensure_ascii=False,
                default=str,
            )
        )
        if selected_newest and consumed + size > history_budget:
            omitted += len(newest_first) - index
            break
        consumed += min(size, history_budget)
        selected_newest.append(turn)
    return list(reversed(selected_newest)), omitted


def _project_conversation_messages(
    turns: list[ConversationTurn],
    history_budget: int,
) -> list[ConversationContextMessage]:
    """Normalize persisted Turns into a deterministic, bounded role projection."""

    raw = _raw_message_payloads(turns)
    if not raw:
        return []
    unbounded = [_conversation_context_message_payload(item, item["content"]) for item in raw]
    rendered_size = len(
        json.dumps(
            unbounded,
            ensure_ascii=False,
            default=str,
        )
    )
    if rendered_size <= history_budget:
        return [ConversationContextMessage.model_validate(item) for item in unbounded]
    else:
        empty = [_conversation_context_message_payload(item, "", truncated=True) for item in raw]
        overhead = len(json.dumps(empty, ensure_ascii=False, default=str))
        available = max(1, history_budget - overhead - len(raw))
        allocations = _allocate_content_chars(
            [len(str(item["content"])) for item in raw],
            available,
        )
        bounded_contents = [
            _truncate_text(str(item["content"]), allocation)
            for item, allocation in zip(raw, allocations, strict=True)
        ]

    return [
        _conversation_context_message(
            item,
            content,
            truncated=content != item["content"],
        )
        for item, content in zip(raw, bounded_contents, strict=True)
    ]


def _conversation_context_message(
    item: dict[str, Any],
    content: str,
    *,
    truncated: bool = False,
) -> ConversationContextMessage:
    return ConversationContextMessage.model_validate(
        _conversation_context_message_payload(item, content, truncated=truncated)
    )


def _conversation_context_message_payload(
    item: dict[str, Any],
    content: str,
    *,
    truncated: bool = False,
) -> dict[str, Any]:
    return {
        "role": item["role"],
        "content": content,
        "source_ref": item["source_ref"],
        "source_kind": "conversation_turn",
        "trust": "untrusted_data",
        "turn_id": item["turn_id"],
        "sequence": item["sequence"],
        "content_hash": hashlib.sha256(content.encode()).hexdigest(),
        "content_truncated": truncated,
    }


def _raw_message_payloads(turns: list[ConversationTurn]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for turn in turns:
        source_ref = f"conversation-turn:{turn.id}"
        payloads.extend(
            (
                {
                    "role": "user",
                    "content": turn.user_input,
                    "source_ref": source_ref,
                    "source_kind": "conversation_turn",
                    "trust": "untrusted_data",
                    "turn_id": str(turn.id),
                    "sequence": turn.sequence,
                },
                {
                    "role": "assistant",
                    "content": _normalize_assistant_output(turn.assistant_output),
                    "source_ref": source_ref,
                    "source_kind": "conversation_turn",
                    "trust": "untrusted_data",
                    "turn_id": str(turn.id),
                    "sequence": turn.sequence,
                },
            )
        )
    return payloads


def _normalize_assistant_output(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("content", "answer"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
        result = value.get("result")
        if isinstance(result, dict) and isinstance(result.get("content"), str):
            return str(result["content"])
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _allocate_content_chars(lengths: list[int], budget: int) -> list[int]:
    allocations = [0] * len(lengths)
    remaining = set(range(len(lengths)))
    available = budget
    while remaining:
        share = available // len(remaining)
        fitting = [index for index in remaining if lengths[index] <= share]
        if not fitting:
            for index in sorted(remaining):
                allocations[index] = share
            for index in sorted(remaining)[: available - share * len(remaining)]:
                allocations[index] += 1
            break
        for index in fitting:
            allocations[index] = lengths[index]
            available -= lengths[index]
            remaining.remove(index)
    return allocations


def _truncate_text(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    if max_chars <= len(_TRUNCATION_SUFFIX):
        return _TRUNCATION_SUFFIX[:max_chars]
    return value[: max_chars - len(_TRUNCATION_SUFFIX)] + _TRUNCATION_SUFFIX
