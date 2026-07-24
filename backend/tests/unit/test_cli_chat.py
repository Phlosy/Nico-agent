from __future__ import annotations

import asyncio
import stat
import threading
from io import StringIO
from os import terminal_size
from pathlib import Path
from typing import Any

import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import fragment_list_to_text
from prompt_toolkit.layout.containers import ConditionalContainer, HSplit, Window
from prompt_toolkit.layout.screen import WritePosition
from prompt_toolkit.utils import get_cwidth

from nico_agent.cli.chat import BottomAnchoredPromptSession, ChatRunner, _chat_style
from nico_agent.cli.chat_session import InteractiveChatSession
from nico_agent.cli.errors import CliError
from nico_agent.cli.output import Output
from nico_agent.cli.slash import parse_slash


class FakeChatClient:
    def __init__(
        self,
        *,
        interrupt: bool = False,
        agents: list[dict[str, Any]] | None = None,
    ) -> None:
        self.interrupt = interrupt
        self.cancelled: list[tuple[str, int]] = []
        self.created: list[str] = []
        self.create_calls: list[dict[str, Any]] = []
        self.agents = [_agent("agent-1", "researcher")] if agents is None else agents
        self.approval_mode = "ask"
        self.agent_permission_updates: list[dict[str, Any]] = []
        self.agent_display_updates: list[dict[str, Any]] = []

    def list_agents(self) -> list[dict[str, Any]]:
        return self.agents

    def get_project(self, project_id: str) -> dict[str, Any]:
        return {"id": project_id, "name": "Test Project"}

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        return next(agent for agent in self.agents if agent["id"] == agent_id)

    def update_agent(self, agent_id: str, **kwargs) -> dict[str, Any]:
        agent = self.get_agent(agent_id)
        if "default_approval_mode" in kwargs:
            self.agent_permission_updates.append({"agent_id": agent_id, **kwargs})
            agent["default_approval_mode"] = kwargs["default_approval_mode"]
            self.approval_mode = kwargs["default_approval_mode"]
        if "show_response_metrics" in kwargs:
            self.agent_display_updates.append({"agent_id": agent_id, **kwargs})
            agent["show_response_metrics"] = kwargs["show_response_metrics"]
        agent["revision"] = int(agent.get("revision") or 1) + 1
        return dict(agent)

    def list_agent_versions(self, agent_id: str) -> list[dict[str, Any]]:
        return [
            {
                "id": "version-1",
                "agent_id": agent_id,
                "version": 1,
                "runtime_provider": "nico_native",
                "model_name": "deepseek-v4-pro",
                "tool_policy": {},
            }
        ]

    def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        return {**_conversation(conversation_id), "approval_mode": self.approval_mode}

    def list_conversations(self, **_kwargs) -> list[dict[str, Any]]:
        return [_conversation("latest")]

    def create_conversation(self, **kwargs) -> dict[str, Any]:
        self.created.append(kwargs["title"])
        self.create_calls.append(kwargs)
        return {
            **_conversation("new"),
            "project_id": kwargs.get("project_id") or "personal-project",
            "agent_id": kwargs["agent_id"],
        }

    def update_conversation(self, conversation_id: str, **kwargs) -> dict[str, Any]:
        if kwargs.get("approval_mode") is not None:
            self.approval_mode = kwargs["approval_mode"]
        return {
            **_conversation(conversation_id),
            "title": kwargs.get("title", "Test"),
            "approval_mode": self.approval_mode,
            "revision": 2,
        }

    def get_conversation_queue(self, conversation_id: str) -> dict[str, Any]:
        return {
            "conversation_id": conversation_id,
            "revision": 1,
            "state": "active",
            "pause_reason": None,
            "pause_turn_id": None,
            "head_turn": None,
            "active_turn": None,
            "pause_turn": None,
            "queued_turns": [],
            "queued_count": 0,
            "capacity": 20,
        }

    def list_conversation_turns(self, _conversation_id: str, **_kwargs) -> list[dict[str, Any]]:
        return []

    def create_conversation_turn(
        self, _conversation_id: str, message: str, **_kwargs
    ) -> dict[str, Any]:
        return {"id": "turn-1", "run_id": "run-1", "user_input": message}

    def stream_run_events(self, _run_id: str, *, on_connection=None):
        del on_connection
        yield {"sequence": 1, "type": "RunStarted", "payload": {}}
        if self.interrupt:
            raise KeyboardInterrupt
        yield {"sequence": 2, "type": "RunCompleted", "payload": {}}

    def get_conversation_turn(self, _turn_id: str) -> dict[str, Any]:
        status = "running" if self.interrupt and not self.cancelled else "completed"
        return {
            "id": "turn-1",
            "run_id": "run-1",
            "run_status": status,
            "run_revision": 3,
            "status": status,
            "assistant_output": {"answer": "done"} if status == "completed" else None,
            "error": None,
        }

    def cancel_conversation_turn(
        self, turn_id: str, *, expected_run_revision: int
    ) -> dict[str, Any]:
        self.cancelled.append((turn_id, expected_run_revision))
        return {
            "id": turn_id,
            "run_id": "run-1",
            "run_status": "cancelled",
            "run_revision": expected_run_revision + 1,
            "status": "cancelled",
            "assistant_output": None,
            "error": None,
        }


class FakeApprovalClient(FakeChatClient):
    def __init__(self) -> None:
        super().__init__()
        self.decisions: list[dict[str, Any]] = []

    def stream_run_events(self, _run_id: str, *, on_connection=None):
        del on_connection
        yield {"sequence": 1, "type": "RunStarted", "payload": {}}
        yield {
            "sequence": 2,
            "type": "ApprovalRequested",
            "payload": {"approval_id": "approval-1"},
        }
        if self.decisions:
            yield {"sequence": 3, "type": "ToolApprovalApproved", "payload": {}}
            yield {"sequence": 4, "type": "RunCompleted", "payload": {}}

    def get_tool_approval(self, approval_id: str) -> dict[str, Any]:
        return {
            "id": approval_id,
            "run_id": "run-1",
            "tool_name": "database.read",
            "tool_version": "1.0.0",
            "risk_level": "medium",
            "status": "requested",
            "requester": "runtime:nico_native",
            "arguments": {"query": "SELECT count(*) FROM prices", "password": "[REDACTED]"},
            "expires_at": "2026-07-19T12:30:00Z",
            "revision": 1,
        }

    def decide_tool_approval(self, approval_id: str, **kwargs) -> dict[str, Any]:
        self.decisions.append({"approval_id": approval_id, **kwargs})
        return {
            **self.get_tool_approval(approval_id),
            "status": "approved",
            "allowed_scope": kwargs["allowed_scope"],
            "revision": 2,
        }

    def get_conversation_turn(self, _turn_id: str) -> dict[str, Any]:
        completed = bool(self.decisions)
        return {
            "id": "turn-1",
            "run_id": "run-1",
            "run_status": "completed" if completed else "waiting_for_approval",
            "run_revision": 3,
            "status": "completed" if completed else "waiting_for_approval",
            "assistant_output": {"answer": "done"} if completed else None,
            "error": None,
        }


class FakeRetryApprovalClient(FakeApprovalClient):
    def list_conversation_turns(self, _conversation_id: str, **_kwargs) -> list[dict[str, Any]]:
        return [
            {
                "id": "failed-turn",
                "run_id": "failed-run",
                "run_status": "failed",
                "run_revision": 7,
            }
        ]

    def retry_conversation_turn(self, turn_id: str, **kwargs) -> dict[str, Any]:
        assert turn_id == "failed-turn"
        assert kwargs == {
            "expected_run_id": "failed-run",
            "expected_run_revision": 7,
        }
        return {"id": "turn-1", "run_id": "run-1"}

    def get_conversation_queue(self, conversation_id: str) -> dict[str, Any]:
        failed = {
            "id": "failed-turn",
            "run_id": "failed-run",
            "run_status": "failed",
            "run_revision": 7,
        }
        return {
            "conversation_id": conversation_id,
            "revision": 2,
            "state": "paused",
            "pause_reason": "run_failed",
            "pause_turn_id": "failed-turn",
            "head_turn": None,
            "active_turn": None,
            "pause_turn": failed,
            "queued_turns": [],
            "queued_count": 0,
            "capacity": 20,
        }


class FakeNoisyChatClient(FakeChatClient):
    def stream_run_events(self, _run_id: str, *, on_connection=None):
        if on_connection is not None:
            on_connection("reconnecting", 1, 3)
            on_connection("recovered", 1, 3)
        yield {"sequence": 1, "type": "TaskCreated", "payload": {}}
        yield {"sequence": 2, "type": "RuntimeModelCallStarted", "payload": {}}
        yield {
            "sequence": 3,
            "type": "RuntimeModelOutputDelta",
            "payload": {"message": "private intermediate output"},
        }
        yield {"sequence": 4, "type": "RunCompleted", "payload": {}}


class RecordingProgress:
    def __init__(self, actions: list[str]) -> None:
        self.actions = actions

    def __enter__(self):
        self.actions.append("progress:start")
        return self

    def __exit__(self, *_args) -> None:
        self.stop()

    def event(self, event: dict[str, Any]) -> None:
        self.actions.append(f"progress:event:{event['type']}")

    def connection(self, state: str, _attempt=None, _maximum=None) -> None:
        self.actions.append(f"progress:connection:{state}")

    def pause(self) -> None:
        self.actions.append("progress:pause")

    def resume(self) -> None:
        self.actions.append("progress:resume")

    def stop(self) -> None:
        if not self.actions or self.actions[-1] != "progress:stop":
            self.actions.append("progress:stop")


class FakeProjectChatClient(FakeChatClient):
    def __init__(self) -> None:
        super().__init__()
        self.guidance: list[dict[str, Any]] = []
        self.escalations: list[dict[str, Any]] = []
        self.withdrawals: list[dict[str, Any]] = []

    def list_conversation_turns(self, _conversation_id: str, **_kwargs) -> list[dict[str, Any]]:
        return [
            {
                "id": "turn-1",
                "run_id": "run-1",
                "run_status": "running",
                "run_revision": 3,
            }
        ]

    def create_run_intervention(self, project_id: str, session_id: str, run_id: str, **kwargs):
        value = {"project_id": project_id, "session_id": session_id, "run_id": run_id, **kwargs}
        self.guidance.append(value)
        return {"id": "intervention-1", "status": "pending", "revision": 1}

    def escalate_project_change(self, project_id: str, session_id: str, **kwargs):
        value = {"project_id": project_id, "session_id": session_id, **kwargs}
        self.escalations.append(value)
        return {"id": "turn-lead", "run_id": "run-lead"}

    def list_run_interventions(self, _project_id: str, _session_id: str, _run_id: str):
        return [{"id": "intervention-1", "status": "pending", "revision": 2}]

    def withdraw_run_intervention(
        self,
        project_id: str,
        session_id: str,
        run_id: str,
        intervention_id: str,
        **kwargs,
    ):
        value = {
            "project_id": project_id,
            "session_id": session_id,
            "run_id": run_id,
            "intervention_id": intervention_id,
            **kwargs,
        }
        self.withdrawals.append(value)
        return {"id": intervention_id, "status": "withdrawn", "revision": 3}


class FakeQueueChatClient(FakeChatClient):
    def __init__(self, *, paused: bool = False) -> None:
        super().__init__()
        self.paused = paused
        self.permission_updates: list[dict[str, Any]] = []
        self.resume_calls: list[dict[str, Any]] = []
        self.retries: list[tuple[str, str, int]] = []
        self.inspected_runs: list[str] = []

    @staticmethod
    def _turn(sequence: int, status: str) -> dict[str, Any]:
        return {
            "id": f"turn-{sequence}",
            "sequence": sequence,
            "run_id": f"run-{sequence}",
            "run_status": status,
            "run_revision": sequence + 3,
            "status": "queued" if status == "pending" else status,
            "user_input": f"message {sequence}",
        }

    def get_conversation_queue(self, conversation_id: str) -> dict[str, Any]:
        active = None if self.paused else self._turn(1, "running")
        pause = self._turn(1, "failed") if self.paused else None
        queued = [self._turn(2, "pending"), self._turn(3, "pending")]
        return {
            "conversation_id": conversation_id,
            "revision": 7,
            "state": "paused" if self.paused else "active",
            "pause_reason": "run_failed" if self.paused else None,
            "pause_turn_id": pause["id"] if pause else None,
            "head_turn": queued[0] if self.paused else active,
            "active_turn": active,
            "pause_turn": pause,
            "queued_turns": queued,
            "queued_count": len(queued),
            "capacity": 20,
        }

    def update_conversation(self, conversation_id: str, **kwargs) -> dict[str, Any]:
        self.permission_updates.append(kwargs)
        return super().update_conversation(conversation_id, **kwargs)

    def resume_conversation_queue(self, conversation_id: str, **kwargs) -> dict[str, Any]:
        self.resume_calls.append({"conversation_id": conversation_id, **kwargs})
        self.paused = False
        return self.get_conversation_queue(conversation_id)

    def retry_conversation_turn(self, turn_id: str, **kwargs) -> dict[str, Any]:
        self.retries.append((turn_id, kwargs["expected_run_id"], kwargs["expected_run_revision"]))
        return {"id": turn_id, "run_id": "retry-run"}

    def stream_run_events(self, _run_id: str, *, on_connection=None):
        del on_connection
        yield {"sequence": 1, "type": "RunCompleted", "payload": {}}

    def get_conversation_turn(self, turn_id: str) -> dict[str, Any]:
        return {
            **self._turn(1, "completed"),
            "id": turn_id,
            "assistant_output": {"answer": "retried"},
        }

    def list_run_tool_calls(self, run_id: str) -> list[dict[str, Any]]:
        self.inspected_runs.append(run_id)
        return []

    def list_tool_approvals(self, *, run_id=None, **_kwargs) -> list[dict[str, Any]]:
        if run_id is not None:
            self.inspected_runs.append(str(run_id))
        return []

    def get_run(self, run_id: str) -> dict[str, Any]:
        return {"id": run_id, "task_id": f"task-{run_id}"}


class FakeInteractiveChatClient(FakeQueueChatClient):
    def __init__(self) -> None:
        super().__init__()
        self.approval_mode = "auto-all"
        self.submitted_messages: list[str] = []
        self.decisions: list[dict[str, Any]] = []
        self.user_input_answers: list[dict[str, Any]] = []

    def get_runtime(self, run_id: str) -> dict[str, Any]:
        assert run_id == "run-1"
        return {
            "execution_manifest": {
                "model": "deepseek-v4-pro",
                "tool_approval_policy": {"mode": "ask"},
            }
        }

    def list_tool_approvals(self, **_kwargs) -> list[dict[str, Any]]:
        return []

    def list_user_inputs(self, **_kwargs) -> list[dict[str, Any]]:
        return []

    def create_conversation_turn(
        self, _conversation_id: str, message: str, **_kwargs
    ) -> dict[str, Any]:
        self.submitted_messages.append(message)
        sequence = 3 + len(self.submitted_messages)
        return {
            "id": f"turn-{sequence}",
            "run_id": f"run-{sequence}",
            "run_status": "pending",
            "run_revision": 0,
            "sequence": sequence,
            "user_input": message,
        }

    def decide_tool_approval(self, approval_id: str, **kwargs) -> dict[str, Any]:
        self.decisions.append({"approval_id": approval_id, **kwargs})
        return {"id": approval_id, "status": kwargs["decision"], "revision": 2}

    def answer_user_input(self, request_id: str, **kwargs) -> dict[str, Any]:
        self.user_input_answers.append({"request_id": request_id, **kwargs})
        return {"id": request_id, "status": "answered", "revision": 2}


class BlockingWatchClient:
    def __init__(self) -> None:
        self.release = threading.Event()
        self.started = threading.Event()
        self.closed = False

    def stream_run_events(self, _run_id: str, *, on_connection=None):
        del on_connection
        self.started.set()
        self.release.wait(timeout=5)
        if False:
            yield {}

    def close(self) -> None:
        self.closed = True
        self.release.set()


class FailingWatchClient(BlockingWatchClient):
    def stream_run_events(self, _run_id: str, *, on_connection=None):
        del on_connection
        raise RuntimeError("client closed during stream setup")
        yield


def _conversation(value: str) -> dict[str, Any]:
    return {
        "id": value,
        "project_id": "project-1",
        "agent_id": "agent-1",
        "title": "Test",
        "agent_version_id": "version-1",
        "status": "active",
        "revision": 1,
        "last_turn_id": None,
    }


def _agent(value: str, name: str, *, status: str = "ready") -> dict[str, Any]:
    return {
        "id": value,
        "name": name,
        "display_name": name.title(),
        "status": status,
        "current_version_id": f"version-{value}",
        "default_approval_mode": "ask",
        "show_response_metrics": False,
        "revision": 1,
    }


def _runner(client: FakeChatClient, tmp_path: Path, *, json_mode: bool = True) -> ChatRunner:
    return ChatRunner(
        client,  # type: ignore[arg-type]
        Output(json_mode=json_mode, no_color=True, stdout=StringIO(), stderr=StringIO()),
        history_path=tmp_path / "history",
    )


def test_chat_resolve_new_resume_and_continue(tmp_path: Path) -> None:
    client = FakeChatClient()
    runner = _runner(client, tmp_path)

    assert (
        runner.resolve(
            project_id="p",
            agent_id="agent-1",
            agent_version_id=None,
            resume_id=None,
            continue_latest=False,
            title="Fresh",
        )["id"]
        == "new"
    )
    assert (
        runner.resolve(
            project_id=None,
            agent_id=None,
            agent_version_id=None,
            resume_id="saved",
            continue_latest=False,
            title="ignored",
        )["id"]
        == "saved"
    )
    assert (
        runner.resolve(
            project_id=None,
            agent_id=None,
            agent_version_id=None,
            resume_id=None,
            continue_latest=True,
            title="ignored",
        )["id"]
        == "latest"
    )
    assert client.created == ["Fresh"]


def test_bare_chat_uses_the_only_ready_agent_and_personal_mode(tmp_path: Path) -> None:
    client = FakeChatClient(
        agents=[
            _agent("draft", "draft-agent", status="draft"),
            _agent("ready", "ready-agent"),
        ]
    )
    runner = _runner(client, tmp_path, json_mode=False)

    conversation = runner.resolve(
        project_id=None,
        agent_id=None,
        agent_version_id=None,
        resume_id=None,
        continue_latest=False,
        title="Personal",
        interactive=False,
    )

    assert conversation["agent_id"] == "ready"
    assert conversation["_cli_mode"] == "personal"
    assert client.create_calls[0]["mode"] == "personal"
    assert client.create_calls[0]["project_id"] is None
    runner.renderer.header(
        {
            "agent": "Ready Agent",
            "version": "1",
            "runtime": "nico_native",
            "project": None,
            "tools": [],
        }
    )
    assert "Project" not in runner.output.stdout.getvalue()


def test_chat_resolves_exact_agent_name_and_uuid_without_arbitrary_choice(tmp_path: Path) -> None:
    agents = [_agent("agent-1", "researcher"), _agent("agent-2", "writer")]
    client = FakeChatClient(agents=agents)
    runner = _runner(client, tmp_path)

    by_name = runner.resolve(
        project_id=None,
        agent_id="writer",
        agent_version_id=None,
        resume_id=None,
        continue_latest=False,
        title="By name",
        interactive=False,
    )
    by_id = runner.resolve(
        project_id=None,
        agent_id="agent-1",
        agent_version_id=None,
        resume_id=None,
        continue_latest=False,
        title="By ID",
        interactive=False,
    )

    assert by_name["agent_id"] == "agent-2"
    assert by_id["agent_id"] == "agent-1"
    with pytest.raises(CliError, match="matches multiple"):
        _runner(
            FakeChatClient(agents=[_agent("one", "duplicate"), _agent("two", "duplicate")]),
            tmp_path,
        ).resolve(
            project_id=None,
            agent_id="duplicate",
            agent_version_id=None,
            resume_id=None,
            continue_latest=False,
            title="Ambiguous",
            interactive=False,
        )
    with pytest.raises(CliError, match="no ready Agent matches"):
        runner.resolve(
            project_id=None,
            agent_id="missing",
            agent_version_id=None,
            resume_id=None,
            continue_latest=False,
            title="Missing",
            interactive=False,
        )


def test_bare_chat_requires_selector_only_when_multiple_ready_agents(tmp_path: Path) -> None:
    runner = _runner(
        FakeChatClient(agents=[_agent("one", "first"), _agent("two", "second")]),
        tmp_path,
    )
    with pytest.raises(CliError, match="pass --agent"):
        runner.resolve(
            project_id=None,
            agent_id=None,
            agent_version_id=None,
            resume_id=None,
            continue_latest=False,
            title="x",
            interactive=False,
        )
    with pytest.raises(CliError, match="run 'nico setup'"):
        _runner(FakeChatClient(agents=[]), tmp_path).resolve(
            project_id=None,
            agent_id=None,
            agent_version_id=None,
            resume_id=None,
            continue_latest=False,
            title="x",
            interactive=False,
        )


def test_chat_rejects_conflicting_selection(tmp_path: Path) -> None:
    runner = _runner(FakeChatClient(), tmp_path)
    with pytest.raises(CliError, match="cannot be used together"):
        runner.resolve(
            project_id=None,
            agent_id=None,
            agent_version_id=None,
            resume_id="saved",
            continue_latest=True,
            title="x",
        )


def test_one_shot_buffers_json_events_and_ctrl_c_cancels_authoritative_run(
    tmp_path: Path,
) -> None:
    normal = FakeChatClient()
    result = _runner(normal, tmp_path).submit(_conversation("c"), "hello")
    assert result["turn"]["status"] == "completed"
    assert [event["sequence"] for event in result["events"]] == [1, 2]

    interrupted = FakeChatClient(interrupt=True)
    cancelled = _runner(interrupted, tmp_path).submit(_conversation("c"), "stop")
    assert cancelled["turn"]["status"] == "cancelled"
    assert interrupted.cancelled == [("turn-1", 3)]


def test_human_ctrl_c_renders_curated_cancel_without_internal_turn_fields(
    tmp_path: Path,
) -> None:
    client = FakeChatClient(interrupt=True)
    runner = _runner(client, tmp_path, json_mode=False)

    result = runner.submit(_conversation("c"), "stop")

    rendered = runner.output.stdout.getvalue()
    assert result["turn"]["status"] == "cancelled"
    assert "Run cancelled" in rendered
    assert "turn-1" not in rendered
    assert "run-1" not in rendered
    assert "run_revision" not in rendered


def test_human_chat_shows_compact_progress_and_final_without_internal_runtime_events(
    tmp_path: Path,
) -> None:
    runner = _runner(FakeNoisyChatClient(), tmp_path, json_mode=False)

    result = runner.submit(_conversation("c"), "hello")

    rendered = runner.output.stdout.getvalue()
    assert result["turn"]["status"] == "completed"
    assert "› Queued" in rendered
    assert "Reconnecting 1/3" in rendered
    assert "done" in rendered
    assert "private intermediate output" not in rendered
    assert "TaskCreated" not in rendered
    assert "RuntimeModelCallStarted" not in rendered
    assert "RunCompleted" not in rendered


def test_one_shot_chat_renders_persisted_agent_response_metrics(tmp_path: Path) -> None:
    client = FakeChatClient()
    client.agents[0]["show_response_metrics"] = True
    original_get_turn = client.get_conversation_turn

    def measured_turn(turn_id: str) -> dict[str, Any]:
        return {
            **original_get_turn(turn_id),
            "usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
            "created_at": "2026-07-23T08:00:00Z",
            "updated_at": "2026-07-23T08:00:00.75Z",
        }

    client.get_conversation_turn = measured_turn  # type: ignore[method-assign]
    runner = _runner(client, tmp_path, json_mode=False)
    conversation = runner.resolve(
        project_id="project-1",
        agent_id="agent-1",
        agent_version_id=None,
        resume_id=None,
        continue_latest=False,
        title="Measured",
    )
    client.get_agent = lambda _agent_id: pytest.fail(  # type: ignore[method-assign]
        "new conversations should reuse the resolved Agent setting"
    )

    runner.submit(conversation, "hello")

    rendered = runner.output.stdout.getvalue()
    assert "done" in rendered
    assert "0.8s · 8 tokens" in rendered


def test_resumed_one_shot_hydrates_metrics_before_starting_the_run(tmp_path: Path) -> None:
    client = FakeChatClient()
    client.agents[0]["show_response_metrics"] = True
    runner = _runner(client, tmp_path, json_mode=False)
    conversation = runner.resolve(
        project_id=None,
        agent_id=None,
        agent_version_id=None,
        resume_id="conversation-1",
        continue_latest=False,
        title="Resumed",
    )
    original_get_agent = client.get_agent
    agent_reads = 0

    def get_agent_once(agent_id: str) -> dict[str, Any]:
        nonlocal agent_reads
        agent_reads += 1
        if agent_reads > 1:
            pytest.fail("the completed answer must not trigger another Agent read")
        return original_get_agent(agent_id)

    client.get_agent = get_agent_once  # type: ignore[method-assign]

    runner.submit(conversation, "hello")

    assert agent_reads == 1
    assert "tokens unavailable" in runner.output.stdout.getvalue()


def test_chat_history_is_created_with_private_permissions(tmp_path: Path) -> None:
    runner = _runner(FakeChatClient(), tmp_path)

    history_path = runner._secure_history_file()

    assert stat.S_IMODE(history_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(history_path.stat().st_mode) == 0o600


def test_chat_approval_is_server_decided_and_stream_resumes(monkeypatch, tmp_path: Path) -> None:
    client = FakeApprovalClient()
    client.agents[0]["show_response_metrics"] = True
    original_get_turn = client.get_conversation_turn

    def measured_turn(turn_id: str) -> dict[str, Any]:
        return {
            **original_get_turn(turn_id),
            "usage": {"total_tokens": 19},
            "created_at": "2026-07-23T08:00:00Z",
            "updated_at": "2026-07-23T08:00:02Z",
        }

    client.get_conversation_turn = measured_turn  # type: ignore[method-assign]
    stdout = StringIO()
    runner = ChatRunner(
        client,  # type: ignore[arg-type]
        Output(json_mode=False, no_color=True, stdout=stdout, stderr=StringIO()),
        history_path=tmp_path / "history",
        approval_prompt=lambda _message: "2",
    )
    monkeypatch.setattr(
        "nico_agent.cli.chat.sys.stdin",
        type("InteractiveInput", (), {"isatty": lambda self: True})(),
    )

    result = runner.submit(_conversation("c"), "query prices")

    assert result["turn"]["status"] == "completed"
    assert result["approval_required"] is None
    assert client.decisions[0]["decision"] == "approve"
    assert client.decisions[0]["allowed_scope"] == "run"
    assert "Approval required" in stdout.getvalue()
    assert "[REDACTED]" in stdout.getvalue()
    assert "2.0s · 19 tokens" in stdout.getvalue()


def test_chat_progress_pauses_for_approval_resumes_and_stops_before_final(
    monkeypatch, tmp_path: Path
) -> None:
    client = FakeApprovalClient()
    actions: list[str] = []
    runner = ChatRunner(
        client,  # type: ignore[arg-type]
        Output(json_mode=False, no_color=True, stdout=StringIO(), stderr=StringIO()),
        history_path=tmp_path / "history",
        approval_prompt=lambda _message: "1",
    )
    progress = RecordingProgress(actions)
    monkeypatch.setattr(runner.renderer, "progress", lambda: progress)
    monkeypatch.setattr(
        runner.renderer,
        "approval",
        lambda _approval: actions.append("render:approval"),
    )
    monkeypatch.setattr(
        runner.renderer,
        "final",
        lambda _turn, **_kwargs: actions.append("render:final"),
    )
    monkeypatch.setattr(
        "nico_agent.cli.chat.sys.stdin",
        type("InteractiveInput", (), {"isatty": lambda self: True})(),
    )

    runner.submit(_conversation("c"), "query prices")

    assert actions.index("progress:pause") < actions.index("render:approval")
    assert actions.index("render:approval") < actions.index("progress:resume")
    assert actions.index("progress:resume") < actions.index("progress:event:RunCompleted")
    assert actions.index("progress:stop") < actions.index("render:final")


def test_chat_progress_stops_when_final_fetch_fails(monkeypatch, tmp_path: Path) -> None:
    client = FakeChatClient()
    actions: list[str] = []
    runner = _runner(client, tmp_path, json_mode=False)
    monkeypatch.setattr(runner.renderer, "progress", lambda: RecordingProgress(actions))
    monkeypatch.setattr(
        client,
        "get_conversation_turn",
        lambda _turn_id: (_ for _ in ()).throw(CliError("FINAL_FETCH_FAILED", "failed")),
    )

    with pytest.raises(CliError, match="failed"):
        runner.submit(_conversation("c"), "hello")

    assert actions[-1] == "progress:stop"


def test_noninteractive_json_never_auto_approves(tmp_path: Path) -> None:
    client = FakeApprovalClient()

    result = _runner(client, tmp_path, json_mode=True).submit(_conversation("c"), "query prices")

    assert result["turn"]["status"] == "waiting_for_approval"
    assert result["approval_required"]["id"] == "approval-1"
    assert client.decisions == []


def test_pending_human_submit_does_not_render_raw_turn(monkeypatch, tmp_path: Path) -> None:
    client = FakeApprovalClient()
    runner = _runner(client, tmp_path, json_mode=False)
    final_calls: list[dict[str, Any]] = []
    render_final = runner.renderer.final

    def record_final(turn: dict[str, Any], **kwargs: Any) -> None:
        final_calls.append(turn)
        render_final(turn, **kwargs)

    monkeypatch.setattr(runner.renderer, "final", record_final)
    monkeypatch.setattr(
        "nico_agent.cli.chat.sys.stdin",
        type("NonInteractiveInput", (), {"isatty": lambda self: False})(),
    )

    result = runner.submit(_conversation("c"), "query prices")

    rendered = runner.output.stdout.getvalue()
    assert result["turn"]["status"] == "waiting_for_approval"
    assert result["approval_required"]["id"] == "approval-1"
    assert final_calls == []
    assert "turn-1" not in rendered
    assert "run_revision" not in rendered


def test_retry_approval_pauses_decides_resumes_and_cleans_up_before_final(
    monkeypatch, tmp_path: Path
) -> None:
    client = FakeRetryApprovalClient()
    actions: list[str] = []
    runner = ChatRunner(
        client,  # type: ignore[arg-type]
        Output(json_mode=False, no_color=True, stdout=StringIO(), stderr=StringIO()),
        history_path=tmp_path / "history",
        approval_prompt=lambda _message: "1",
    )
    progress = RecordingProgress(actions)
    decide = client.decide_tool_approval

    def record_decision(approval_id: str, **kwargs) -> dict[str, Any]:
        actions.append("client:decision")
        return decide(approval_id, **kwargs)

    monkeypatch.setattr(runner.renderer, "progress", lambda: progress)
    monkeypatch.setattr(
        runner.renderer,
        "approval",
        lambda _approval: actions.append("render:approval"),
    )
    monkeypatch.setattr(
        runner.renderer,
        "final",
        lambda _turn, **_kwargs: actions.append("render:final"),
    )
    monkeypatch.setattr(client, "decide_tool_approval", record_decision)
    monkeypatch.setattr(
        "nico_agent.cli.chat.sys.stdin",
        type("InteractiveInput", (), {"isatty": lambda self: True})(),
    )
    command = parse_slash("/retry")
    assert command is not None

    runner._slash(_conversation("c"), command)

    assert actions.index("progress:pause") < actions.index("render:approval")
    assert actions.index("render:approval") < actions.index("client:decision")
    assert actions.index("client:decision") < actions.index("progress:resume")
    assert actions.index("progress:resume") < actions.index("progress:event:RunCompleted")
    assert actions.index("progress:event:RunCompleted") < actions.index("progress:stop")
    assert actions.index("progress:stop") < actions.index("render:final")


def test_retry_approval_without_decision_stops_without_final(monkeypatch, tmp_path: Path) -> None:
    client = FakeRetryApprovalClient()
    actions: list[str] = []

    def cancel_prompt(_message: str) -> str:
        raise EOFError

    runner = ChatRunner(
        client,  # type: ignore[arg-type]
        Output(json_mode=False, no_color=True, stdout=StringIO(), stderr=StringIO()),
        history_path=tmp_path / "history",
        approval_prompt=cancel_prompt,
    )
    monkeypatch.setattr(runner.renderer, "progress", lambda: RecordingProgress(actions))
    monkeypatch.setattr(
        runner.renderer,
        "approval",
        lambda _approval: actions.append("render:approval"),
    )
    monkeypatch.setattr(
        runner.renderer,
        "final",
        lambda _turn, **_kwargs: actions.append("render:final"),
    )
    monkeypatch.setattr(
        "nico_agent.cli.chat.sys.stdin",
        type("InteractiveInput", (), {"isatty": lambda self: True})(),
    )
    command = parse_slash("/retry")
    assert command is not None

    runner._slash(_conversation("c"), command)

    assert actions[-1] == "progress:stop"
    assert "render:final" not in actions
    assert client.decisions == []


def test_chat_slash_help_and_title_are_real_operations(tmp_path: Path) -> None:
    stdout = StringIO()
    runner = ChatRunner(
        FakeChatClient(),  # type: ignore[arg-type]
        Output(json_mode=False, no_color=True, stdout=stdout, stderr=StringIO()),
        history_path=tmp_path / "history",
    )
    conversation = {
        **_conversation("conversation-1"),
        "_cli_mode": "project",
        "_cli_project_id": "project-1",
        "_cli_project_session_id": "session-1",
    }

    help_command = parse_slash("/help")
    title_command = parse_slash('/title "Research Notes"')
    assert help_command is not None and title_command is not None
    unchanged, should_exit = runner._slash(conversation, help_command)
    updated, _ = runner._slash(unchanged, title_command)

    assert should_exit is False
    assert updated["title"] == "Research Notes"
    assert updated["_cli_mode"] == "project"
    assert updated["_cli_project_id"] == "project-1"
    assert updated["_cli_project_session_id"] == "session-1"
    assert "Nico Slash Commands" in stdout.getvalue()


def test_project_slash_commands_bind_current_session_and_revisions(tmp_path: Path) -> None:
    client = FakeProjectChatClient()
    runner = _runner(client, tmp_path, json_mode=False)
    conversation = {
        **_conversation("conversation-1"),
        "_cli_mode": "project",
        "_cli_project_id": "project-1",
        "_cli_project_session_id": "session-1",
    }

    for raw in (
        "/guide check the failing test",
        "/escalate reduce the project scope",
        "/withdraw intervention-1 no longer needed",
    ):
        command = parse_slash(raw)
        assert command is not None
        runner._slash(conversation, command)

    assert client.guidance[0]["run_id"] == "run-1"
    assert client.guidance[0]["expected_run_revision"] == 3
    assert client.escalations[0]["session_id"] == "session-1"
    assert client.withdrawals[0]["expected_intervention_revision"] == 2


def test_permissions_require_confirmation_only_when_expanding(tmp_path: Path) -> None:
    client = FakeQueueChatClient()
    answers = iter(["y", "3", "n"])
    runner = ChatRunner(
        client,  # type: ignore[arg-type]
        Output(json_mode=False, no_color=True, stdout=StringIO(), stderr=StringIO()),
        history_path=tmp_path / "history",
        selection_prompt=lambda _message: next(answers),
    )
    conversation = {**_conversation("conversation-1"), "approval_mode": "ask"}

    explicit = parse_slash("/permissions auto-medium")
    interactive = parse_slash("/permissions")
    assert explicit is not None and interactive is not None
    changed, _ = runner._slash(conversation, explicit)
    unchanged, _ = runner._slash(changed, interactive)

    assert changed["approval_mode"] == "auto-medium"
    assert unchanged["approval_mode"] == "auto-medium"
    assert client.permission_updates == []
    assert client.agent_permission_updates == [
        {
            "agent_id": "agent-1",
            "expected_revision": 1,
            "default_approval_mode": "auto-medium",
        }
    ]
    assert "Agent Permissions" in runner.output.stdout.getvalue()


def test_permissions_reapply_an_explicit_same_mode_to_the_agent(tmp_path: Path) -> None:
    client = FakeQueueChatClient()
    runner = _runner(client, tmp_path, json_mode=False)
    conversation = {**_conversation("conversation-1"), "approval_mode": "auto-all"}
    command = parse_slash("/permissions ask")
    assert command is not None

    changed, _ = runner._slash(conversation, command)

    assert changed["approval_mode"] == "ask"
    assert client.agent_permission_updates == [
        {
            "agent_id": "agent-1",
            "expected_revision": 1,
            "default_approval_mode": "ask",
        }
    ]


def test_response_metrics_setting_is_persisted_on_the_agent(tmp_path: Path) -> None:
    client = FakeQueueChatClient()
    runner = _runner(client, tmp_path, json_mode=False)
    conversation = _conversation("conversation-1")
    command = parse_slash("/metrics on")
    assert command is not None

    changed, should_exit = runner._slash(conversation, command)

    assert should_exit is False
    assert changed["_cli_show_response_metrics"] is True
    assert client.agent_display_updates == [
        {
            "agent_id": "agent-1",
            "expected_revision": 1,
            "show_response_metrics": True,
        }
    ]
    assert "Response Metrics" in runner.output.stdout.getvalue()


def test_response_metrics_picker_keeps_the_current_agent_setting(tmp_path: Path) -> None:
    client = FakeQueueChatClient()
    client.agents[0]["show_response_metrics"] = True
    runner = ChatRunner(
        client,  # type: ignore[arg-type]
        Output(json_mode=False, no_color=True, stdout=StringIO(), stderr=StringIO()),
        history_path=tmp_path / "history",
        selection_prompt=lambda _message: "",
    )
    command = parse_slash("/metrics")
    assert command is not None

    selected, _ = runner._slash(_conversation("conversation-1"), command)

    assert selected["_cli_show_response_metrics"] is True
    assert client.agent_display_updates == []


def test_response_metrics_can_be_disabled_for_the_agent(tmp_path: Path) -> None:
    client = FakeQueueChatClient()
    client.agents[0]["show_response_metrics"] = True
    runner = _runner(client, tmp_path, json_mode=False)
    command = parse_slash("/metrics off")
    assert command is not None

    changed, should_exit = runner._slash(_conversation("conversation-1"), command)

    assert should_exit is False
    assert changed["_cli_show_response_metrics"] is False
    assert client.agent_display_updates == [
        {
            "agent_id": "agent-1",
            "expected_revision": 1,
            "show_response_metrics": False,
        }
    ]


def test_queue_controls_and_current_commands_use_server_targets(tmp_path: Path) -> None:
    client = FakeQueueChatClient(paused=True)
    runner = _runner(client, tmp_path, json_mode=False)
    conversation = _conversation("conversation-1")

    for raw in ("/queue", "/queue cancel 2", "/retry", "/queue resume"):
        command = parse_slash(raw)
        assert command is not None
        runner._slash(conversation, command)

    assert client.cancelled == [("turn-2", 5)]
    assert client.retries == [("turn-1", "run-1", 4)]
    assert client.resume_calls[0]["expected_revision"] == 7
    assert client.resume_calls[0]["idempotency_key"]

    client.paused = False
    for raw in ("/tools", "/approvals", "/cancel"):
        command = parse_slash(raw)
        assert command is not None
        runner._slash(conversation, command)
    assert client.inspected_runs == ["run-1", "run-1"]
    assert client.cancelled[-1] == ("turn-1", 4)


def test_retry_answer_honors_persisted_agent_response_metrics(tmp_path: Path) -> None:
    client = FakeQueueChatClient(paused=True)
    client.agents[0]["show_response_metrics"] = True
    runner = _runner(client, tmp_path, json_mode=False)
    command = parse_slash("/retry")
    assert command is not None

    runner._slash(_conversation("conversation-1"), command)

    rendered = runner.output.stdout.getvalue()
    assert "retried" in rendered
    assert "time unavailable · tokens unavailable" in rendered


def test_status_shows_full_model_and_current_next_permissions(tmp_path: Path) -> None:
    client = FakeInteractiveChatClient()
    runner = _runner(client, tmp_path, json_mode=False)
    command = parse_slash("/status")
    assert command is not None

    runner._slash(
        {**_conversation("conversation-1"), "approval_mode": "auto-all"},
        command,
    )

    rendered = runner.output.stdout.getvalue()
    assert "deepseek-v4-pro" in rendered
    assert "ask" in rendered
    assert "auto-all" in rendered


async def _run_in_terminal_immediately(callback):
    return callback()


class FakePromptApp:
    def __init__(self, *, text: str = "", cursor_position: int = 0) -> None:
        self.is_running = bool(text)
        self.current_buffer = type(
            "Buffer",
            (),
            {"text": text, "cursor_position": cursor_position},
        )()
        self.result: str | None = None

    def exit(self, *, result: str) -> None:
        self.result = result

    def invalidate(self) -> None:
        return None


class FakePromptSession:
    def __init__(self, app: FakePromptApp | None = None) -> None:
        self.app = app or FakePromptApp()
        self.stream_deltas: list[str] = []
        self.stream_clear_count = 0
        self.activities: list[str | None] = []

    def append_stream_delta(self, delta: str) -> None:
        self.stream_deltas.append(delta)

    def clear_stream(self) -> None:
        self.stream_clear_count += 1

    def set_activity(self, activity: str | None) -> None:
        self.activities.append(activity)


def _interactive_session(
    client: FakeInteractiveChatClient,
    watcher: BlockingWatchClient,
    tmp_path: Path,
    *,
    prompt_session: FakePromptSession | None = None,
    conversation: dict[str, Any] | None = None,
    show_response_metrics: bool = False,
) -> InteractiveChatSession:
    runner = _runner(client, tmp_path, json_mode=False)
    return InteractiveChatSession(
        runner,
        conversation or {**_conversation("conversation-1"), "approval_mode": "auto-all"},
        prompt_session or FakePromptSession(),  # type: ignore[arg-type]
        metadata={
            "model": "configured-model",
            "show_response_metrics": show_response_metrics,
        },
        client_factory=lambda: watcher,  # type: ignore[arg-type,return-value]
    )


async def test_interactive_session_queues_messages_without_waiting_for_active_sse(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("nico_agent.cli.chat_session.run_in_terminal", _run_in_terminal_immediately)
    client = FakeInteractiveChatClient()
    watcher = BlockingWatchClient()
    session = _interactive_session(
        client,
        watcher,
        tmp_path,
        conversation={
            **_conversation("conversation-1"),
            "approval_mode": "auto-all",
            "_cli_mode": "project",
            "_cli_project_id": "project-1",
            "_cli_project_session_id": "session-1",
        },
    )

    await session.initialize()
    assert await asyncio.to_thread(watcher.started.wait, 1)
    first = await session.submit_message("second message")
    second = await session.submit_message("third message")

    assert watcher.release.is_set() is False
    assert [first["sequence"], second["sequence"]] == [4, 5]
    assert client.submitted_messages == ["second message", "third message"]
    rendered = session.runner.output.stdout.getvalue()
    assert "Queued · turn" not in rendered
    prompt = session.composer_prompt()
    prompt_text = fragment_list_to_text(prompt)
    assert prompt_text == "  ↳ queued · message 2\nyou › "
    assert ("class:nico.queue-label", "  ↳ queued · ") in prompt
    assert ("class:nico.user-label", "you › ") in prompt
    footer = session.footer()
    footer_text = fragment_list_to_text(footer)
    assert "deepseek-v4-pro" in footer_text
    assert "ask→auto-all" in footer_text
    assert "work" in footer_text
    assert ("class:bottom-toolbar.model", "deepseek-v4-pro") in footer
    assert ("class:bottom-toolbar.permission", "ask→auto-all") in footer
    await session.close()
    assert watcher.closed is True
    assert client.cancelled == []


async def test_interactive_session_does_not_render_an_idle_submission_notice(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("nico_agent.cli.chat_session.run_in_terminal", _run_in_terminal_immediately)
    client = FakeInteractiveChatClient()
    monkeypatch.setattr(
        client,
        "get_conversation_queue",
        lambda conversation_id: {
            "conversation_id": conversation_id,
            "revision": 1,
            "state": "active",
            "head_turn": None,
            "active_turn": None,
            "pause_turn": None,
            "queued_turns": [],
            "queued_count": 0,
            "capacity": 20,
        },
    )
    watcher = BlockingWatchClient()
    session = _interactive_session(client, watcher, tmp_path)

    submitted = await session.submit_message("first message")

    assert submitted["sequence"] == 4
    rendered = session.runner.output.stdout.getvalue()
    assert rendered == ""
    monkeypatch.setattr(
        "nico_agent.cli.chat_session.shutil.get_terminal_size",
        lambda _fallback: pytest.fail("idle composer should not query terminal size"),
    )
    assert fragment_list_to_text(session.composer_prompt()) == "you › "
    await session.close()


async def test_interactive_composer_uses_fixed_toolbar_and_one_second_refresh(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("nico_agent.cli.chat_session.run_in_terminal", _run_in_terminal_immediately)

    class RecordingPromptSession(FakePromptSession):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[dict[str, Any]] = []

        async def prompt_async(self, _message, **kwargs) -> str:
            self.calls.append(kwargs)
            return "/exit"

    prompt = RecordingPromptSession()
    session = _interactive_session(
        FakeInteractiveChatClient(),
        BlockingWatchClient(),
        tmp_path,
        prompt_session=prompt,
    )

    await session.run()

    assert callable(prompt.calls[0]["bottom_toolbar"])
    assert prompt.calls[0]["refresh_interval"] == 1.0


async def test_interactive_composer_keeps_running_after_an_unknown_slash_command(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("nico_agent.cli.chat_session.run_in_terminal", _run_in_terminal_immediately)

    class SequencePromptSession(FakePromptSession):
        def __init__(self) -> None:
            super().__init__()
            self.messages = iter(["/per", "/exit"])
            self.calls = 0

        async def prompt_async(self, _message, **_kwargs) -> str:
            self.calls += 1
            return next(self.messages)

    prompt = SequencePromptSession()
    session = _interactive_session(
        FakeInteractiveChatClient(),
        BlockingWatchClient(),
        tmp_path,
        prompt_session=prompt,
    )

    await session.run()

    assert prompt.calls == 2
    assert "UNKNOWN_SLASH_COMMAND" in session.runner.output.stderr.getvalue()


async def test_interactive_composer_persists_submitted_user_messages_explicitly(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("nico_agent.cli.chat_session.run_in_terminal", _run_in_terminal_immediately)

    class SequencePromptSession(FakePromptSession):
        def __init__(self) -> None:
            super().__init__()
            self.messages = iter(["排队的问题", "/exit"])

        async def prompt_async(self, _message, **_kwargs) -> str:
            return next(self.messages)

    session = _interactive_session(
        FakeInteractiveChatClient(),
        BlockingWatchClient(),
        tmp_path,
        prompt_session=SequencePromptSession(),
    )

    await session.run()

    assert "you › 排队的问题" in session.runner.output.stdout.getvalue()


async def test_interactive_prompt_layout_anchors_stream_queue_input_and_toolbar_to_bottom() -> None:
    prompt = BottomAnchoredPromptSession(bottom_toolbar="status")
    root = prompt.app.layout.container

    assert isinstance(root, HSplit)
    assert isinstance(root.children[0], ConditionalContainer)
    assert isinstance(root.children[1], ConditionalContainer)
    assert isinstance(root.children[1].content, Window)
    assert root.children[1].content.height.weight > 0
    assert isinstance(root.children[2], ConditionalContainer)
    assert isinstance(root.children[3], HSplit)
    with set_app(prompt.app):
        heights = root._divide_heights(WritePosition(xpos=0, ypos=0, width=80, height=20))
    assert heights is not None
    assert heights[-1] == 2
    assert 18 in heights
    await prompt.app.cancel_and_wait_for_background_tasks()


def test_interactive_prompt_renders_curated_activity_above_the_composer() -> None:
    prompt = BottomAnchoredPromptSession()

    prompt.set_activity("Running Reading file · 0:03")

    assert fragment_list_to_text(prompt._activity_fragments()) == (
        "  • Running Reading file · 0:03"
    )
    prompt.set_activity(None)
    assert prompt._activity_text() == ""


def test_interactive_prompt_sanitizes_streamed_terminal_controls() -> None:
    prompt = BottomAnchoredPromptSession()

    prompt.append_stream_delta("hello\x1b[2J\tworld\rnext")

    assert prompt._stream_output == "hello[2J    world\nnext"


async def test_interactive_session_streams_only_user_visible_output_and_clears_before_final(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("nico_agent.cli.chat_session.run_in_terminal", _run_in_terminal_immediately)
    prompt = FakePromptSession()
    session = _interactive_session(
        FakeInteractiveChatClient(),
        BlockingWatchClient(),
        tmp_path,
        prompt_session=prompt,
    )
    session._watch_run_id = "run-1"
    session._consumer_task = asyncio.create_task(session._consume_background())

    for event in (
        {
            "type": "RuntimeModelOutputDelta",
            "payload": {
                "payload": {
                    "call_key": "planner:1",
                    "delta": "private plan",
                    "visibility": "internal",
                }
            },
        },
        {
            "type": "RuntimeModelOutputDelta",
            "payload": {
                "payload": {
                    "call_key": "model:1",
                    "delta": "你好",
                    "visibility": "assistant",
                }
            },
        },
        {
            "type": "RuntimeModelOutputDelta",
            "payload": {
                "payload": {
                    "call_key": "model:1",
                    "delta": "，世界",
                    "visibility": "assistant",
                }
            },
        },
    ):
        await session._background.put(("event", ("run-1", event)))
    await session._background.put(
        (
            "finished",
            (
                {
                    "id": "turn-1",
                    "assistant_output": {"answer": "你好，世界"},
                },
                {
                    "state": "active",
                    "queued_turns": [],
                    "queued_count": 0,
                    "capacity": 20,
                },
                "run-1",
            ),
        )
    )
    for _ in range(100):
        if session._watch_run_id is None:
            break
        await asyncio.sleep(0)

    assert prompt.stream_deltas == ["你好", "，世界"]
    assert prompt.stream_clear_count == 1
    assert prompt.activities[-1] is None
    assert "你好，世界" in session.runner.output.stdout.getvalue()
    assert "private plan" not in session.runner.output.stdout.getvalue()
    await session.close()


async def test_interactive_answer_renders_enabled_agent_metrics_after_streaming(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("nico_agent.cli.chat_session.run_in_terminal", _run_in_terminal_immediately)
    session = _interactive_session(
        FakeInteractiveChatClient(),
        BlockingWatchClient(),
        tmp_path,
        show_response_metrics=True,
    )
    session._watch_run_id = "run-1"
    session._consumer_task = asyncio.create_task(session._consume_background())
    await session._background.put(
        (
            "finished",
            (
                {
                    "id": "turn-1",
                    "assistant_output": {"answer": "Measured streamed answer"},
                    "usage": {"total_tokens": 77},
                    "created_at": "2026-07-23T08:00:00Z",
                    "updated_at": "2026-07-23T08:00:03.25Z",
                },
                {
                    "state": "active",
                    "queued_turns": [],
                    "queued_count": 0,
                    "capacity": 20,
                },
                "run-1",
            ),
        )
    )
    for _ in range(100):
        if session._watch_run_id is None:
            break
        await asyncio.sleep(0)

    rendered = session.runner.output.stdout.getvalue()
    assert "Measured streamed answer" in rendered
    assert "3.2s · 77 tokens" in rendered
    await session.close()


async def test_interactive_activity_clears_after_watch_error(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("nico_agent.cli.chat_session.run_in_terminal", _run_in_terminal_immediately)
    prompt = FakePromptSession()
    session = _interactive_session(
        FakeInteractiveChatClient(),
        BlockingWatchClient(),
        tmp_path,
        prompt_session=prompt,
    )
    session._watch_run_id = "run-1"
    session.progress = session.runner.renderer.progress(initial="Thinking", clock=lambda: 10.0)
    session._sync_activity()
    session._consumer_task = asyncio.create_task(session._consume_background())

    await session._background.put(("error", ("run-1", CliError("WATCH_FAILED", "watch failed"))))
    for _ in range(100):
        if session._watch_run_id is None:
            break
        await asyncio.sleep(0)

    assert prompt.activities[-1] is None
    assert "WATCH_FAILED" in session.runner.output.stderr.getvalue()
    await session.close()


def test_interactive_composer_truncates_and_flattens_the_next_queued_message(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "nico_agent.cli.chat_session.shutil.get_terminal_size",
        lambda _fallback: terminal_size((32, 24)),
    )
    session = _interactive_session(
        FakeInteractiveChatClient(),
        BlockingWatchClient(),
        tmp_path,
    )
    session.queue["queued_turns"] = [
        {
            "user_input": (
                "请先分析第一段内容\n然后继续分析第二段非常非常长的内容，但是不要在预览里完整显示"
            )
        }
    ]

    next_line, user_line = fragment_list_to_text(session.composer_prompt()).splitlines()

    assert next_line.startswith("  ↳ queued · 请先分析第一段内容")
    assert next_line.endswith("…")
    assert get_cwidth(next_line) <= 32
    assert user_line == "you › "


def test_interactive_composer_hides_the_current_head_and_previews_its_successor() -> None:
    session = _interactive_session(
        FakeInteractiveChatClient(),
        BlockingWatchClient(),
        Path("/tmp"),
    )
    current = {
        "id": "turn-4",
        "run_id": "run-4",
        "run_status": "pending",
        "user_input": "你能干什么",
    }
    successor = {
        "id": "turn-5",
        "run_id": "run-5",
        "run_status": "pending",
        "user_input": "下一条消息",
    }
    session.queue.update(
        {
            "head_turn": current,
            "active_turn": None,
            "queued_turns": [current, successor],
            "queued_count": 2,
        }
    )

    prompt = fragment_list_to_text(session.composer_prompt())
    assert prompt == "  ↳ queued · 下一条消息\nyou › "
    assert "你能干什么" not in prompt


async def test_interactive_activity_tracks_thinking_and_tool_work(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("nico_agent.cli.chat_session.run_in_terminal", _run_in_terminal_immediately)
    prompt = FakePromptSession()
    session = _interactive_session(
        FakeInteractiveChatClient(),
        BlockingWatchClient(),
        tmp_path,
        prompt_session=prompt,
    )
    session._watch_run_id = "run-1"
    session.progress = session.runner.renderer.progress(initial="Preparing", clock=lambda: 10.0)
    session._consumer_task = asyncio.create_task(session._consume_background())

    await session._background.put(
        ("event", ("run-1", {"type": "RuntimeModelCallStarted", "payload": {}}))
    )
    await session._background.put(
        (
            "event",
            (
                "run-1",
                {
                    "type": "ToolCallStarted",
                    "payload": {"tool_call_id": "tool-1", "tool": "file.read@1.0.0"},
                },
            ),
        )
    )
    for _ in range(100):
        if any(value and "Reading file" in value for value in prompt.activities):
            break
        await asyncio.sleep(0)

    assert any(value and value.startswith("Thinking") for value in prompt.activities)
    assert any(value and "Running Reading file" in value for value in prompt.activities)
    await session.close()


def test_interactive_activity_provider_renders_live_elapsed_and_reconnect(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "nico_agent.cli.chat_session.shutil.get_terminal_size",
        lambda _fallback: terminal_size((80, 24)),
    )
    now = [10.0]
    prompt = BottomAnchoredPromptSession()
    session = _interactive_session(
        FakeInteractiveChatClient(),
        BlockingWatchClient(),
        tmp_path,
        prompt_session=prompt,
    )
    session._watch_run_id = "run-1"
    session.progress = session.runner.renderer.progress(
        initial="Preparing",
        clock=lambda: now[0],
    )
    session._sync_activity()

    assert "Preparing · 0:00" in fragment_list_to_text(prompt._activity_fragments())
    now[0] = 13.0
    session.progress.connection("reconnecting", 2, 4)
    activity = fragment_list_to_text(prompt._activity_fragments())
    assert "0:03" in activity
    assert "reconnecting 2/4" in activity


def test_interactive_footer_shows_live_phase_and_safe_tool_activity(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "nico_agent.cli.chat_session.shutil.get_terminal_size",
        lambda _fallback: terminal_size((120, 24)),
    )
    session = _interactive_session(
        FakeInteractiveChatClient(),
        BlockingWatchClient(),
        tmp_path,
    )
    session._watch_run_id = "run-1"
    session.progress = session.runner.renderer.progress(initial="Preparing", clock=lambda: 10.0)

    assert fragment_list_to_text(session.composer_prompt()) == "you › "
    assert "Preparing · 0:00" in fragment_list_to_text(session.footer())

    session.progress.event({"type": "RuntimeModelCallStarted", "payload": {}})
    assert "Thinking · 0:00" in fragment_list_to_text(session.footer())

    session.progress.event(
        {
            "type": "ToolCallStarted",
            "payload": {"tool_call_id": "tool-1", "tool": "file.read@1.0.0"},
        }
    )
    assert "Running Reading file · 0:00" in fragment_list_to_text(session.footer())

    session._watch_run_id = None
    footer = fragment_list_to_text(session.footer())
    assert "Idle" in footer
    assert "Reading file" not in footer


async def test_interactive_approval_preserves_exact_draft_and_rejects_invalid_choice(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("nico_agent.cli.chat_session.run_in_terminal", _run_in_terminal_immediately)
    client = FakeInteractiveChatClient()
    watcher = BlockingWatchClient()
    prompt = FakePromptSession(FakePromptApp(text="unfinished 草稿", cursor_position=5))
    session = _interactive_session(client, watcher, tmp_path, prompt_session=prompt)
    approval = {"id": "approval-1", "revision": 7}

    session._interrupt_for_approval()
    assert session.saved_draft is not None
    assert session.saved_draft.text == "unfinished 草稿"
    assert session.saved_draft.cursor_position == 5
    assert await session.decide_approval(approval, "not-a-choice") is False
    assert client.decisions == []
    assert await session.decide_approval(approval, "2") is True
    assert client.decisions[0]["decision"] == "approve"
    assert client.decisions[0]["allowed_scope"] == "run"
    assert session.saved_draft.text == "unfinished 草稿"
    assert session.saved_draft.cursor_position == 5
    await session.close()


async def test_approval_discovered_during_refresh_interrupts_the_active_composer(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("nico_agent.cli.chat_session.run_in_terminal", _run_in_terminal_immediately)
    client = FakeInteractiveChatClient()
    monkeypatch.setattr(
        client,
        "list_tool_approvals",
        lambda **_kwargs: [{"id": "approval-2", "revision": 1}],
    )
    prompt = FakePromptSession(FakePromptApp(text="queued draft", cursor_position=6))
    session = _interactive_session(
        client,
        BlockingWatchClient(),
        tmp_path,
        prompt_session=prompt,
    )

    await session._refresh_state(include_approvals=True)

    assert prompt.app.result == "\0nico-approval-interrupt"
    assert session.saved_draft is not None
    assert session.saved_draft.text == "queued draft"
    assert session.saved_draft.cursor_position == 6
    assert [approval["id"] for approval in session._approvals] == ["approval-2"]
    await session.close()


async def test_pending_approval_interrupt_waits_for_composer_start(tmp_path: Path) -> None:
    client = FakeInteractiveChatClient()
    prompt = FakePromptSession(FakePromptApp())
    session = _interactive_session(
        client,
        BlockingWatchClient(),
        tmp_path,
        prompt_session=prompt,
    )
    waiter = asyncio.create_task(session._interrupt_composer_on_approval())

    session._offer_approval({"id": "approval-race", "revision": 1})
    await asyncio.sleep(0)
    assert prompt.app.result is None

    prompt.app.is_running = True
    await asyncio.wait_for(waiter, timeout=1)
    assert prompt.app.result == "\0nico-approval-interrupt"
    await session.close()


async def test_restart_discovers_agent_question_before_composer_and_answer_is_not_a_turn(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("nico_agent.cli.chat_session.run_in_terminal", _run_in_terminal_immediately)
    client = FakeInteractiveChatClient()
    question = {
        "id": "question-1",
        "run_id": "run-1",
        "question": "Which target should I use?",
        "reason": "Several targets remain equally plausible.",
        "input_schema": {"type": "string", "enum": ["file", "database"]},
        "status": "requested",
        "revision": 1,
        "expires_at": "2026-07-24T00:00:00Z",
    }
    monkeypatch.setattr(client, "list_user_inputs", lambda **_kwargs: [question])
    prompt = FakePromptSession()
    session = _interactive_session(
        client,
        BlockingWatchClient(),
        tmp_path,
        prompt_session=prompt,
    )

    await session.initialize()

    assert [item["id"] for item in session._user_inputs] == ["question-1"]
    assert client.submitted_messages == []
    assert await session.answer_user_input(question, "database") is True
    assert client.submitted_messages == []
    assert client.user_input_answers[0]["request_id"] == "question-1"
    assert client.user_input_answers[0]["expected_revision"] == 1
    assert client.user_input_answers[0]["answer"] == "database"
    await session.close()


async def test_agent_question_has_distinct_interrupt_and_owns_input_before_approval(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("nico_agent.cli.chat_session.run_in_terminal", _run_in_terminal_immediately)
    client = FakeInteractiveChatClient()
    question = {
        "id": "question-owner",
        "run_id": "run-1",
        "question": "Choose one",
        "reason": "Needed to continue.",
        "input_schema": {"type": "string"},
        "status": "requested",
        "revision": 1,
    }
    monkeypatch.setattr(client, "list_user_inputs", lambda **_kwargs: [question])
    monkeypatch.setattr(
        client,
        "list_tool_approvals",
        lambda **_kwargs: [{"id": "approval-owner", "revision": 1}],
    )
    prompt = FakePromptSession(FakePromptApp(text="ordinary composer draft", cursor_position=8))
    session = _interactive_session(
        client,
        BlockingWatchClient(),
        tmp_path,
        prompt_session=prompt,
    )

    await session._refresh_state(include_approvals=True)

    assert prompt.app.result == "\0nico-user-input-interrupt"
    assert session.saved_draft is not None
    assert session.saved_draft.text == "ordinary composer draft"
    assert session._next_interrupt_owner() == "user_input"
    assert len(session._approvals) == 1
    assert client.submitted_messages == []
    await session.close()


async def test_interactive_ctrl_c_targets_only_active_head_and_detach_does_not_cancel_queue(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("nico_agent.cli.chat_session.run_in_terminal", _run_in_terminal_immediately)
    client = FakeInteractiveChatClient()
    watcher = BlockingWatchClient()
    session = _interactive_session(client, watcher, tmp_path)

    await session.initialize()
    await session.cancel_active_turn()

    assert client.cancelled == [("turn-1", 4)]
    rendered = session.runner.output.stdout.getvalue()
    assert "Run cancelled" in rendered
    assert "turn-1" not in rendered
    assert "run-1" not in rendered
    await session.close()
    assert client.cancelled == [("turn-1", 4)]


async def test_interactive_ctrl_c_cancels_unclaimed_head_but_not_paused_successor(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("nico_agent.cli.chat_session.run_in_terminal", _run_in_terminal_immediately)
    client = FakeInteractiveChatClient()
    watcher = BlockingWatchClient()
    pending = client._turn(1, "pending")
    queue = {
        "conversation_id": "conversation-1",
        "revision": 8,
        "state": "active",
        "pause_reason": None,
        "head_turn": pending,
        "active_turn": None,
        "pause_turn": None,
        "queued_turns": [pending],
        "queued_count": 1,
        "capacity": 20,
    }
    monkeypatch.setattr(client, "get_conversation_queue", lambda _conversation_id: queue)
    session = _interactive_session(client, watcher, tmp_path)

    await session.initialize()
    await session.cancel_active_turn()

    assert client.cancelled == [("turn-1", 4)]
    await session.close()

    paused_client = FakeInteractiveChatClient()
    paused_client.paused = True
    monkeypatch.setattr(
        paused_client,
        "get_runtime",
        lambda _run_id: {"execution_manifest": {}},
    )
    paused_session = _interactive_session(paused_client, BlockingWatchClient(), tmp_path)
    await paused_session.initialize()
    await paused_session.cancel_active_turn()
    assert paused_client.cancelled == []
    await paused_session.close()


async def test_approval_detail_failure_does_not_stop_background_consumer(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("nico_agent.cli.chat_session.run_in_terminal", _run_in_terminal_immediately)
    monkeypatch.setattr("nico_agent.cli.chat_session._APPROVAL_FETCH_RETRY_SECONDS", 0)

    class FailingApprovalClient(FakeInteractiveChatClient):
        def __init__(self) -> None:
            super().__init__()
            self.approval_fetches = 0

        def get_tool_approval(self, _approval_id: str) -> dict[str, Any]:
            self.approval_fetches += 1
            raise CliError(
                "APPROVAL_DETAIL_UNAVAILABLE",
                "approval details are temporarily unavailable",
            )

    client = FailingApprovalClient()
    session = _interactive_session(client, BlockingWatchClient(), tmp_path)
    session._watch_run_id = "run-1"
    session._consumer_task = asyncio.create_task(session._consume_background())
    await session._background.put(
        (
            "event",
            (
                "run-1",
                {
                    "type": "ApprovalRequested",
                    "payload": {"approval_id": "approval-1"},
                },
            ),
        )
    )
    for _ in range(100):
        if session.runner.output.stderr.getvalue():
            break
        await asyncio.sleep(0)

    assert client.approval_fetches == 3
    assert session._consumer_task.done() is False
    assert "APPROVAL_DETAIL_UNAVAILABLE" in session.runner.output.stderr.getvalue()
    assert not session._approvals
    await session.close()


async def test_interactive_watcher_converts_transport_races_to_safe_cli_errors(
    tmp_path: Path,
) -> None:
    client = FakeInteractiveChatClient()
    watcher = FailingWatchClient()
    session = _interactive_session(client, watcher, tmp_path)
    session._loop = asyncio.get_running_loop()

    await asyncio.to_thread(
        session._watch_blocking,
        "run-1",
        "turn-1",
        "conversation-1",
    )
    kind, value = await session._background.get()

    assert kind == "error"
    run_id, error = value
    assert run_id == "run-1"
    assert error.code == "CHAT_WATCH_FAILED"
    assert "client closed" not in error.message
    await session.close()


def test_interactive_tty_uses_async_session_controller(monkeypatch, tmp_path: Path) -> None:
    runner = _runner(FakeChatClient(), tmp_path, json_mode=False)
    called: dict[str, Any] = {}
    prompt_options: dict[str, Any] = {}

    class RecordingSessionController:
        def __init__(self, selected_runner, conversation, prompt_session, *, metadata) -> None:
            called.update(
                {
                    "runner": selected_runner,
                    "conversation": conversation,
                    "prompt_session": prompt_session,
                    "metadata": metadata,
                }
            )

        async def run(self) -> None:
            called["ran"] = True

    monkeypatch.setattr("nico_agent.cli.chat.InteractiveChatSession", RecordingSessionController)

    def prompt_session(**kwargs):
        prompt_options.update(kwargs)
        return FakePromptSession()

    monkeypatch.setattr("nico_agent.cli.chat.BottomAnchoredPromptSession", prompt_session)
    monkeypatch.setattr(
        runner,
        "_metadata",
        lambda _conversation: {
            "agent": "Researcher",
            "version": 1,
            "runtime": "nico_native",
            "model": "deepseek-v4-pro",
            "tools": [],
        },
    )
    monkeypatch.setattr(
        "nico_agent.cli.chat.sys.stdin",
        type("InteractiveInput", (), {"isatty": lambda self: True})(),
    )

    conversation = _conversation("conversation-1")
    runner.run_interactive(conversation, read_only=False)

    assert called["ran"] is True
    assert called["conversation"] == conversation
    assert called["metadata"]["model"] == "deepseek-v4-pro"
    assert prompt_options["erase_when_done"] is True
    completions = list(
        prompt_options["completer"].get_completions(
            Document("/"), CompleteEvent(completion_requested=True)
        )
    )
    assert any(
        completion.text == "/help" and completion.display_meta_text == "显示命令帮助"
        for completion in completions
    )


def test_chat_theme_styles_completion_menu_and_footer_without_reverse_video() -> None:
    color = _chat_style(no_color=False)
    selected = color.get_attrs_for_style_str("class:completion-menu.completion.current")
    user = color.get_attrs_for_style_str("class:nico.user-label")
    queue = color.get_attrs_for_style_str("class:nico.queue-label")
    toolbar = color.get_attrs_for_style_str("class:bottom-toolbar")
    model = color.get_attrs_for_style_str("class:bottom-toolbar.model")
    permission = color.get_attrs_for_style_str("class:bottom-toolbar.permission")
    activity = color.get_attrs_for_style_str("class:bottom-toolbar.activity")

    assert selected.bgcolor == "334957"
    assert selected.color == "f0c66b"
    assert user.color == "8fb3cc"
    assert queue.color == "7895ac"
    assert toolbar.bgcolor == "ansidefault"
    assert model.color == "f0c66b"
    assert permission.color == "8fb3cc"
    assert activity.color == "c8d4dc"

    no_color = _chat_style(no_color=True)
    assert no_color.get_attrs_for_style_str("class:bottom-toolbar").reverse is False
