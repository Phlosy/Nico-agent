"""Deterministic OpenAI-compatible server for native-runtime acceptance profiles."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import AsyncIterator

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from nico_agent.models.tool_names import provider_safe_tool_name

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


def _require_token(value: str | None) -> None:
    expected = os.environ.get("NICO_FAKE_MODEL_TOKEN", "goal-g-fake-token")
    if value != expected:
        raise HTTPException(status_code=401, detail="invalid model credential")


@app.get("/openai/v1/models")
async def openai_models(authorization: str | None = Header(default=None)) -> dict:
    expected = os.environ.get("NICO_FAKE_MODEL_TOKEN", "goal-g-fake-token")
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="invalid model credential")
    return {"object": "list", "data": [{"id": "fake-openai", "object": "model"}]}


@app.post("/openai/v1/chat/completions")
async def onboarding_openai_chat(
    command: ChatRequest,
    authorization: str | None = Header(default=None),
) -> StreamingResponse:
    return await chat_completions(command, authorization)


@app.get("/anthropic/v1/models")
async def anthropic_models(x_api_key: str | None = Header(default=None)) -> dict:
    _require_token(x_api_key)
    return {
        "data": [{"id": "fake-anthropic", "display_name": "Fake Anthropic"}],
        "has_more": False,
    }


@app.post("/anthropic/v1/messages")
async def anthropic_messages(
    request: Request,
    x_api_key: str | None = Header(default=None),
) -> StreamingResponse:
    _require_token(x_api_key)
    payload = await request.json()
    if not payload.get("stream"):
        raise HTTPException(status_code=400, detail="streaming is required")

    async def chunks() -> AsyncIterator[str]:
        events = (
            {
                "type": "message_start",
                "message": {"usage": {"input_tokens": 4}},
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": "Nico Anthropic "},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "is ready."},
            },
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 5},
            },
            {"type": "message_stop"},
        )
        for event in events:
            yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"

    return StreamingResponse(
        chunks(),
        media_type="text/event-stream",
        headers={"request-id": "nico-fake-anthropic-request"},
    )


@app.get("/gemini/v1beta/models")
async def gemini_models(x_goog_api_key: str | None = Header(default=None)) -> dict:
    _require_token(x_goog_api_key)
    return {
        "models": [
            {
                "name": "models/fake-gemini",
                "displayName": "Fake Gemini",
                "supportedGenerationMethods": ["generateContent"],
            }
        ]
    }


@app.post("/gemini/v1beta/models/{model}:streamGenerateContent")
async def gemini_generate(
    model: str,
    x_goog_api_key: str | None = Header(default=None),
) -> StreamingResponse:
    _require_token(x_goog_api_key)
    if model != "fake-gemini":
        raise HTTPException(status_code=404, detail="model not found")

    async def chunks() -> AsyncIterator[str]:
        events = (
            {"candidates": [{"content": {"parts": [{"text": "Nico Gemini "}]}}]},
            {
                "candidates": [
                    {
                        "content": {"parts": [{"text": "is ready."}]},
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 4,
                    "candidatesTokenCount": 5,
                    "totalTokenCount": 9,
                },
            },
        )
        for event in events:
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        chunks(),
        media_type="text/event-stream",
        headers={"x-request-id": "nico-fake-gemini-request"},
    )


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
    _require_provider_safe_tool_names(command)

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
        elif command.model == "web-e2e-fake":
            values = _web_e2e_chunks(command, tool_observations)
        elif _has_tool(command, "file.write") and tool_observations == 0:
            values = _tool_chunks(
                call_id="goal-h-file-write",
                name=provider_safe_tool_name("file.write"),
                arguments={
                    "path": "goal-h/recovery-proof.txt",
                    "content": "written exactly once before worker recovery\n",
                },
            )
        elif _has_tool(command, "python.execute") and tool_observations == 1:
            values = _tool_chunks(
                call_id="goal-h-python",
                name=provider_safe_tool_name("python.execute"),
                arguments={"code": "result = {'sum': sum(range(6)), 'proof': 'sandbox'}"},
            )
        elif tool_observations >= 2:
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


def _web_e2e_chunks(command: ChatRequest, tool_observations: int) -> list[dict]:
    if tool_observations == 0:
        return _tool_chunks(
            call_id="web-e2e-search",
            name=provider_safe_tool_name("web.search"),
            arguments={"query": "current Nico offline evidence", "count": 1},
        )
    observations = _tool_observation_payloads(command.messages)
    if tool_observations == 1:
        search = observations[-1]
        output = search.get("output")
        results = output.get("results") if isinstance(output, dict) else None
        if not isinstance(results, list) or not results or not isinstance(results[0], dict):
            raise HTTPException(status_code=422, detail="search observation has no result")
        url = results[0].get("url")
        tool_call_id = search.get("tool_call_id")
        if not isinstance(url, str) or not isinstance(tool_call_id, str):
            raise HTTPException(status_code=422, detail="search observation is incomplete")
        return _tool_chunks(
            call_id="web-e2e-fetch",
            name=provider_safe_tool_name("web.fetch"),
            arguments={"url": url, "search_tool_call_id": tool_call_id},
        )
    fetched = observations[-1].get("output")
    final_url = fetched.get("final_url") if isinstance(fetched, dict) else None
    if not isinstance(final_url, str):
        raise HTTPException(status_code=422, detail="fetch observation has no final URL")
    return _text_chunks(f"Verified offline Web evidence: {final_url}")


def _tool_observation_payloads(messages: list[dict]) -> list[dict]:
    payloads: list[dict] = []
    for message in messages:
        if message.get("role") != "tool" or not isinstance(message.get("content"), str):
            continue
        try:
            payload = json.loads(message["content"])
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=422, detail="tool observation is not JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="tool observation is not an object")
        payloads.append(payload)
    return payloads


def _has_tool(command: ChatRequest, name: str) -> bool:
    wire_name = provider_safe_tool_name(name)
    return any(
        isinstance(tool, dict) and tool.get("function", {}).get("name") == wire_name
        for tool in command.tools
    )


def _require_provider_safe_tool_names(command: ChatRequest) -> None:
    for tool in command.tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        if not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) is None:
            raise HTTPException(status_code=422, detail="invalid provider Tool function name")


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
