"""Deterministic OpenAI-compatible server for native-runtime acceptance profiles."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Nico Native Runtime Fake Model", docs_url=None, redoc_url=None)


class ChatRequest(BaseModel):
    model: str = Field(min_length=1)
    messages: list[dict]
    stream: bool
    stream_options: dict = Field(default_factory=dict)
    tools: list[dict] = Field(default_factory=list)
    response_format: dict | None = None


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat_completions(
    command: ChatRequest,
    authorization: str | None = Header(default=None),
) -> StreamingResponse:
    expected = os.environ.get("NICO_FAKE_MODEL_TOKEN", "goal-g-fake-token")
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="invalid model credential")
    if not command.stream:
        raise HTTPException(status_code=400, detail="streaming is required")

    async def chunks() -> AsyncIterator[str]:
        tool_observations = sum(message.get("role") == "tool" for message in command.messages)
        schema_name = _schema_name(command.response_format)
        if schema_name == "nico_plan":
            revision = _runtime_value(command.messages, "revision", 1)
            step_key = "draft" if revision == 1 else "correct"
            values = _text_chunks(
                json.dumps(
                    {
                        "objective": "Produce a verified Goal I report",
                        "steps": [
                            {
                                "key": step_key,
                                "title": step_key.title(),
                                "instruction": f"Execute {step_key}",
                                "acceptance": {"non_empty": True},
                                "depends_on": [],
                            }
                        ],
                    },
                    separators=(",", ":"),
                )
            )
        elif schema_name == "nico_plan_step_output":
            step_key = _runtime_value(command.messages, "step", {}).get("key")
            content = "" if step_key == "draft" else "Goal I verified report"
            values = _text_chunks(json.dumps({"output": {"content": content}}))
        elif schema_name == "nico_reflection":
            values = _text_chunks(
                json.dumps(
                    {
                        "decision": "replan",
                        "reason": "The draft failed deterministic validation.",
                        "recovery_instruction": "Create a non-empty corrected report.",
                    },
                    separators=(",", ":"),
                )
            )
        elif schema_name == "nico_completion_judge":
            values = _text_chunks(
                '{"verdict":"passed","reason":"The corrected report satisfies acceptance."}'
            )
        elif command.model == "goal-k-fake":
            values = _goal_k_chunks(command)
        elif command.model == "goal-j-fake":
            values = _goal_j_chunks(command, tool_observations)
        elif command.tools and tool_observations == 0:
            values = _tool_chunks(
                call_id="goal-h-file-write",
                name="file.write",
                arguments={
                    "path": "goal-h/recovery-proof.txt",
                    "content": "written exactly once before worker recovery\n",
                },
            )
        elif command.tools and tool_observations == 1:
            values = _tool_chunks(
                call_id="goal-h-python",
                name="python.execute",
                arguments={"code": "result = {'sum': sum(range(6)), 'proof': 'sandbox'}"},
            )
        elif command.tools:
            values = _text_chunks("Nico ReAct recovered safely after two tool observations.")
        else:
            values = _text_chunks("Nico native runtime is ready.")
        if (
            command.model == "goal-j-fake"
            and tool_observations == 0
            and not _has_tool(command, "delegate_agent")
        ):
            await asyncio.sleep(_goal_j_delay())
        for value in values:
            yield f"data: {json.dumps(value)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        chunks(),
        media_type="text/event-stream",
        headers={"X-Request-ID": "nico-goal-g-fake-request"},
    )


def _goal_j_chunks(command: ChatRequest, tool_observations: int) -> list[dict]:
    tool_names = {
        tool.get("function", {}).get("name") for tool in command.tools if isinstance(tool, dict)
    }
    task_input = _task_input(command.messages)
    if "delegate_agent" in tool_names and tool_observations == 0:
        targets = task_input.get("target_agent_version_ids", [])
        if not isinstance(targets, list) or len(targets) != 2:
            raise HTTPException(status_code=422, detail="Goal J requires two child versions")
        calls = [
            {
                "id": f"goal-j-child-{index}",
                "name": "delegate_agent",
                "arguments": {
                    "target_agent_version_id": target,
                    "objective": f"Research independent branch {index}",
                    "acceptance": {"required": ["content"]},
                    "context_refs": ["task:input"],
                    "budget": {
                        "token_limit": 300,
                        "cost_limit_microunits": 0,
                        "tool_call_limit": 0,
                    },
                    "execution_mode": "parallel",
                },
            }
            for index, target in enumerate(targets)
        ]
        return _multi_tool_chunks(calls)
    if "delegate_agent" in tool_names:
        return _text_chunks("Goal J aggregated two child results and Artifact references.")
    if "store_artifact" in tool_names and tool_observations == 0:
        objective = str(task_input.get("objective", "child finding"))
        return _tool_chunks(
            call_id="goal-j-artifact",
            name="store_artifact",
            arguments={
                "name": "goal-j-finding.txt",
                "content_type": "text/plain",
                "content_text": f"Artifact evidence for {objective}",
                "share_with_parent": True,
            },
        )
    return _text_chunks(f"child-result: {task_input.get('objective', 'completed')}")


def _goal_k_chunks(command: ChatRequest) -> list[dict]:
    rendered = "\n".join(
        str(message.get("content", "")) for message in command.messages if isinstance(message, dict)
    )
    has_memory = '"kind":"published_memory"' in rendered or '"kind": "published_memory"' in rendered
    has_skill = '"kind":"published_skill"' in rendered or '"kind": "published_skill"' in rendered
    if has_memory and has_skill:
        return _text_chunks("Goal K used one published Memory and one published Skill.")
    return _text_chunks(
        "Goal K source evidence: reviewed knowledge must be published before runtime reuse."
    )


def _has_tool(command: ChatRequest, name: str) -> bool:
    return any(
        isinstance(tool, dict) and tool.get("function", {}).get("name") == name
        for tool in command.tools
    )


def _goal_j_delay() -> float:
    try:
        delay = float(os.environ.get("NICO_FAKE_MODEL_GOAL_J_DELAY_SECONDS", "0"))
    except ValueError:
        return 0
    return min(max(delay, 0), 5)


def _task_input(messages: list[dict]) -> dict:
    for message in reversed(messages):
        if message.get("role") != "user" or not isinstance(message.get("content"), str):
            continue
        candidate = message["content"].rsplit("\n\n", 1)[-1]
        try:
            envelope = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        task = envelope.get("task")
        if isinstance(task, dict) and isinstance(task.get("input"), dict):
            return task["input"]
    return {}


def _schema_name(response_format: dict | None) -> str | None:
    if not isinstance(response_format, dict):
        return None
    schema = response_format.get("json_schema")
    return schema.get("name") if isinstance(schema, dict) else None


def _runtime_value(messages: list[dict], key: str, default):
    for message in reversed(messages):
        content = message.get("content")
        if not isinstance(content, str) or "UNTRUSTED DATA:" not in content:
            continue
        try:
            envelope = json.loads(content.split("UNTRUSTED DATA:\n", 1)[1])
        except (json.JSONDecodeError, IndexError):
            continue
        runtime = envelope.get("runtime")
        if isinstance(runtime, dict):
            return runtime.get(key, default)
    return default


def _tool_chunks(*, call_id: str, name: str, arguments: dict) -> list[dict]:
    return [
        {
            "id": "nico-native-fake",
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(arguments, separators=(",", ":")),
                                },
                            }
                        ]
                    }
                }
            ],
        },
        {
            "id": "nico-native-fake",
            "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 12, "total_tokens": 52},
        },
    ]


def _multi_tool_chunks(calls: list[dict]) -> list[dict]:
    return [
        {
            "id": "nico-native-fake",
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": index,
                                "id": call["id"],
                                "type": "function",
                                "function": {
                                    "name": call["name"],
                                    "arguments": json.dumps(
                                        call["arguments"], separators=(",", ":")
                                    ),
                                },
                            }
                            for index, call in enumerate(calls)
                        ]
                    }
                }
            ],
        },
        {
            "id": "nico-native-fake",
            "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 24, "total_tokens": 74},
        },
    ]


def _text_chunks(content: str) -> list[dict]:
    midpoint = max(1, len(content) // 2)
    return [
        {"id": "nico-native-fake", "choices": [{"delta": {"content": content[:midpoint]}}]},
        {"id": "nico-native-fake", "choices": [{"delta": {"content": content[midpoint:]}}]},
        {
            "id": "nico-native-fake",
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 48, "completion_tokens": 10, "total_tokens": 58},
        },
    ]
