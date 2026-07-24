"""Versioned compact checkpoint encoding and integrity validation."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ReactCheckpoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal[2] = 2
    provider_name: Literal["nico_native"] = "nico_native"
    protocol_version: Literal["2.0"] = "2.0"
    execution_mode: Literal["react"] = "react"
    manifest_hash: str = Field(min_length=64, max_length=64)
    loop_state: Literal[
        "reasoning",
        "waiting_for_tool",
        "waiting_for_user_input",
        "waiting_for_subagent",
        "observing",
        "completed",
        "failed",
        "cancelled",
    ]
    iteration: int = Field(ge=1)
    context_version: int = Field(ge=0)
    context_hash: str | None = Field(default=None, min_length=64, max_length=64)
    last_model_call_key: str | None = Field(default=None, max_length=200)
    last_action_batch_key: str | None = Field(default=None, min_length=64, max_length=64)
    history: tuple[dict[str, Any], ...] = ()
    pending_actions: tuple[dict[str, Any], ...] = ()
    completed_action_keys: tuple[str, ...] = ()
    tool_calls_consumed: int = Field(default=0, ge=0)
    coordination_calls_consumed: int = Field(default=0, ge=0)
    waiting_delegations: tuple[dict[str, Any], ...] = ()
    consumed_message_ids: tuple[str, ...] = ()
    pending_user_input: dict[str, Any] | None = None
    consumed_user_input_ids: tuple[str, ...] = ()
    observed_web_urls: tuple[str, ...] = Field(default=(), max_length=1024)
    citation_repair_attempted: bool = False
    citation_provisional_output: dict[str, Any] | None = None
    clarification_correction_attempted: bool = False
    clarification_source_batch_key: str | None = Field(default=None, min_length=64, max_length=64)
    clarification_observation: dict[str, Any] | None = None
    semantic_final_correction_attempted: bool = False
    semantic_final_source_batch_key: str | None = Field(default=None, min_length=64, max_length=64)
    semantic_final_observation: dict[str, Any] | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    checkpoint_hash: str = Field(min_length=64, max_length=64)


class PlanCheckpoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal[3] = 3
    provider_name: Literal["nico_native"] = "nico_native"
    protocol_version: Literal["2.0"] = "2.0"
    execution_mode: Literal["plan_and_execute"] = "plan_and_execute"
    manifest_hash: str = Field(min_length=64, max_length=64)
    loop_state: Literal[
        "planning",
        "executing",
        "waiting_for_user_input",
        "reflecting",
        "finalizing",
        "completed",
        "failed",
        "cancelled",
    ]
    plan_revision: int = Field(default=0, ge=0)
    plan_content_hash: str | None = Field(default=None, min_length=64, max_length=64)
    current_step_key: str | None = Field(default=None, max_length=64)
    completed_step_keys: tuple[str, ...] = ()
    latest_output: dict[str, Any] | None = None
    last_action_batch_key: str | None = Field(default=None, min_length=64, max_length=64)
    recovery_instruction: str | None = Field(default=None, max_length=8_000)
    recovery_decision: Literal["retry", "replan", "fail"] | None = None
    step_state: dict[str, Any] | None = None
    context_version: int = Field(default=0, ge=0)
    reflection_count: int = Field(default=0, ge=0)
    pending_user_input: dict[str, Any] | None = None
    consumed_user_input_ids: tuple[str, ...] = ()
    observed_web_urls: tuple[str, ...] = Field(default=(), max_length=1024)
    citation_repair_attempted: bool = False
    citation_provisional_output: dict[str, Any] | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    checkpoint_hash: str = Field(min_length=64, max_length=64)


class SetupProofCheckpoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    provider_name: Literal["nico_native"] = "nico_native"
    protocol_version: Literal["2.0"] = "2.0"
    execution_mode: Literal["setup_proof"] = "setup_proof"
    manifest_hash: str = Field(min_length=64, max_length=64)
    loop_state: Literal[
        "searching",
        "fetching",
        "finalizing",
        "completed",
        "failed",
        "cancelled",
    ]
    search_tool_call_id: str | None = Field(default=None, min_length=36, max_length=36)
    source_url: str | None = Field(default=None, min_length=1, max_length=4096)
    final_url: str | None = Field(default=None, min_length=1, max_length=4096)
    completed_action_keys: tuple[str, ...] = ()
    tool_calls_consumed: int = Field(default=0, ge=0, le=2)
    usage: dict[str, Any] = Field(default_factory=dict)
    checkpoint_hash: str = Field(min_length=64, max_length=64)


def direct_checkpoint(
    *,
    context_hash: str,
    call_key: str,
    completed: bool,
    usage: dict[str, Any],
    action_batch_key: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "execution_mode": "direct",
        "loop_state": "completed" if completed else "reasoning",
        "iteration": 1,
        "context_hash": context_hash,
        "last_model_call_key": call_key,
        "last_action_batch_key": action_batch_key,
        "usage": usage,
    }


def new_react_checkpoint(manifest: dict[str, Any]) -> ReactCheckpoint:
    return make_react_checkpoint(
        manifest=manifest,
        loop_state="reasoning",
        iteration=1,
        context_version=0,
    )


def make_react_checkpoint(
    *,
    manifest: dict[str, Any],
    loop_state: str,
    iteration: int,
    context_version: int,
    context_hash: str | None = None,
    last_model_call_key: str | None = None,
    last_action_batch_key: str | None = None,
    history: tuple[dict[str, Any], ...] = (),
    pending_actions: tuple[dict[str, Any], ...] = (),
    completed_action_keys: tuple[str, ...] = (),
    tool_calls_consumed: int = 0,
    coordination_calls_consumed: int = 0,
    waiting_delegations: tuple[dict[str, Any], ...] = (),
    consumed_message_ids: tuple[str, ...] = (),
    pending_user_input: dict[str, Any] | None = None,
    consumed_user_input_ids: tuple[str, ...] = (),
    observed_web_urls: tuple[str, ...] = (),
    citation_repair_attempted: bool = False,
    citation_provisional_output: dict[str, Any] | None = None,
    clarification_correction_attempted: bool = False,
    clarification_source_batch_key: str | None = None,
    clarification_observation: dict[str, Any] | None = None,
    semantic_final_correction_attempted: bool = False,
    semantic_final_source_batch_key: str | None = None,
    semantic_final_observation: dict[str, Any] | None = None,
    usage: dict[str, Any] | None = None,
) -> ReactCheckpoint:
    payload = {
        "schema_version": 2,
        "provider_name": "nico_native",
        "protocol_version": "2.0",
        "execution_mode": "react",
        "manifest_hash": _hash(manifest),
        "loop_state": loop_state,
        "iteration": iteration,
        "context_version": context_version,
        "context_hash": context_hash,
        "last_model_call_key": last_model_call_key,
        "last_action_batch_key": last_action_batch_key,
        "history": history,
        "pending_actions": pending_actions,
        "completed_action_keys": completed_action_keys,
        "tool_calls_consumed": tool_calls_consumed,
        "coordination_calls_consumed": coordination_calls_consumed,
        "waiting_delegations": waiting_delegations,
        "consumed_message_ids": consumed_message_ids,
        "pending_user_input": pending_user_input,
        "consumed_user_input_ids": consumed_user_input_ids,
        "observed_web_urls": observed_web_urls,
        "citation_repair_attempted": citation_repair_attempted,
        "citation_provisional_output": citation_provisional_output,
        "clarification_correction_attempted": clarification_correction_attempted,
        "clarification_source_batch_key": clarification_source_batch_key,
        "clarification_observation": clarification_observation,
        "semantic_final_correction_attempted": semantic_final_correction_attempted,
        "semantic_final_source_batch_key": semantic_final_source_batch_key,
        "semantic_final_observation": semantic_final_observation,
        "usage": usage or {},
    }
    payload["checkpoint_hash"] = _hash(payload)
    return ReactCheckpoint.model_validate(payload)


def load_react_checkpoint(
    value: dict[str, Any] | None,
    *,
    manifest: dict[str, Any],
) -> ReactCheckpoint:
    if value is None:
        return new_react_checkpoint(manifest)
    try:
        checkpoint = ReactCheckpoint.model_validate(value)
    except ValidationError as exc:
        raise ValueError("checkpoint schema is invalid") from exc
    original_payload = dict(value)
    expected = original_payload.pop("checkpoint_hash")
    if _hash(original_payload) != expected:
        raise ValueError("checkpoint integrity hash does not match")
    if checkpoint.manifest_hash != _hash(manifest):
        raise ValueError("checkpoint execution manifest does not match")
    return checkpoint


def make_plan_checkpoint(
    *,
    manifest: dict[str, Any],
    loop_state: str,
    plan_revision: int = 0,
    plan_content_hash: str | None = None,
    current_step_key: str | None = None,
    completed_step_keys: tuple[str, ...] = (),
    latest_output: dict[str, Any] | None = None,
    last_action_batch_key: str | None = None,
    recovery_instruction: str | None = None,
    recovery_decision: str | None = None,
    step_state: dict[str, Any] | None = None,
    context_version: int = 0,
    reflection_count: int = 0,
    pending_user_input: dict[str, Any] | None = None,
    consumed_user_input_ids: tuple[str, ...] = (),
    observed_web_urls: tuple[str, ...] = (),
    citation_repair_attempted: bool = False,
    citation_provisional_output: dict[str, Any] | None = None,
    usage: dict[str, Any] | None = None,
) -> PlanCheckpoint:
    payload = {
        "schema_version": 3,
        "provider_name": "nico_native",
        "protocol_version": "2.0",
        "execution_mode": "plan_and_execute",
        "manifest_hash": _hash(manifest),
        "loop_state": loop_state,
        "plan_revision": plan_revision,
        "plan_content_hash": plan_content_hash,
        "current_step_key": current_step_key,
        "completed_step_keys": completed_step_keys,
        "latest_output": latest_output,
        "last_action_batch_key": last_action_batch_key,
        "recovery_instruction": recovery_instruction,
        "recovery_decision": recovery_decision,
        "step_state": step_state,
        "context_version": context_version,
        "reflection_count": reflection_count,
        "pending_user_input": pending_user_input,
        "consumed_user_input_ids": consumed_user_input_ids,
        "observed_web_urls": observed_web_urls,
        "citation_repair_attempted": citation_repair_attempted,
        "citation_provisional_output": citation_provisional_output,
        "usage": usage or {},
    }
    payload["checkpoint_hash"] = _hash(payload)
    return PlanCheckpoint.model_validate(payload)


def load_plan_checkpoint(
    value: dict[str, Any] | None,
    *,
    manifest: dict[str, Any],
) -> PlanCheckpoint:
    if value is None:
        return make_plan_checkpoint(manifest=manifest, loop_state="planning")
    try:
        checkpoint = PlanCheckpoint.model_validate(value)
    except ValidationError as exc:
        raise ValueError("checkpoint schema is invalid") from exc
    original_payload = dict(value)
    expected = original_payload.pop("checkpoint_hash")
    if _hash(original_payload) != expected:
        raise ValueError("checkpoint integrity hash does not match")
    if checkpoint.manifest_hash != _hash(manifest):
        raise ValueError("checkpoint execution manifest does not match")
    return checkpoint


def make_setup_proof_checkpoint(
    *,
    manifest: dict[str, Any],
    loop_state: str,
    search_tool_call_id: str | None = None,
    source_url: str | None = None,
    final_url: str | None = None,
    completed_action_keys: tuple[str, ...] = (),
    tool_calls_consumed: int = 0,
    usage: dict[str, Any] | None = None,
) -> SetupProofCheckpoint:
    payload = {
        "schema_version": 1,
        "provider_name": "nico_native",
        "protocol_version": "2.0",
        "execution_mode": "setup_proof",
        "manifest_hash": _hash(manifest),
        "loop_state": loop_state,
        "search_tool_call_id": search_tool_call_id,
        "source_url": source_url,
        "final_url": final_url,
        "completed_action_keys": completed_action_keys,
        "tool_calls_consumed": tool_calls_consumed,
        "usage": usage or {"model_calls": 0, "tool_calls": tool_calls_consumed},
    }
    payload["checkpoint_hash"] = _hash(payload)
    return SetupProofCheckpoint.model_validate(payload)


def load_setup_proof_checkpoint(
    value: dict[str, Any] | None,
    *,
    manifest: dict[str, Any],
) -> SetupProofCheckpoint:
    if value is None:
        return make_setup_proof_checkpoint(manifest=manifest, loop_state="searching")
    try:
        checkpoint = SetupProofCheckpoint.model_validate(value)
    except ValidationError as exc:
        raise ValueError("checkpoint schema is invalid") from exc
    original_payload = dict(value)
    expected = original_payload.pop("checkpoint_hash")
    if _hash(original_payload) != expected:
        raise ValueError("checkpoint integrity hash does not match")
    if checkpoint.manifest_hash != _hash(manifest):
        raise ValueError("checkpoint execution manifest does not match")
    return checkpoint


def _hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
