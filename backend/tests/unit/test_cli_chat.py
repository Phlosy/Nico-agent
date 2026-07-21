from __future__ import annotations

import stat
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from nico_agent.cli.chat import ChatRunner
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

    def list_agents(self) -> list[dict[str, Any]]:
        return self.agents

    def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        return _conversation(conversation_id)

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
        return {**_conversation(conversation_id), "title": kwargs["title"], "revision": 2}

    def list_conversation_turns(self, _conversation_id: str, **_kwargs) -> list[dict[str, Any]]:
        return []

    def create_conversation_turn(
        self, _conversation_id: str, message: str, **_kwargs
    ) -> dict[str, Any]:
        return {"id": "turn-1", "run_id": "run-1", "user_input": message}

    def stream_run_events(self, _run_id: str):
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

    def stream_run_events(self, _run_id: str):
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


def test_chat_history_is_created_with_private_permissions(tmp_path: Path) -> None:
    runner = _runner(FakeChatClient(), tmp_path)

    history_path = runner._secure_history_file()

    assert stat.S_IMODE(history_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(history_path.stat().st_mode) == 0o600


def test_chat_approval_is_server_decided_and_stream_resumes(monkeypatch, tmp_path: Path) -> None:
    client = FakeApprovalClient()
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
    assert "Sensitive tool approval" in stdout.getvalue()
    assert "[REDACTED]" in stdout.getvalue()


def test_noninteractive_json_never_auto_approves(tmp_path: Path) -> None:
    client = FakeApprovalClient()

    result = _runner(client, tmp_path, json_mode=True).submit(_conversation("c"), "query prices")

    assert result["turn"]["status"] == "waiting_for_approval"
    assert result["approval_required"]["id"] == "approval-1"
    assert client.decisions == []


def test_chat_slash_help_and_title_are_real_operations(tmp_path: Path) -> None:
    stdout = StringIO()
    runner = ChatRunner(
        FakeChatClient(),  # type: ignore[arg-type]
        Output(json_mode=False, no_color=True, stdout=stdout, stderr=StringIO()),
        history_path=tmp_path / "history",
    )
    conversation = _conversation("conversation-1")

    help_command = parse_slash("/help")
    title_command = parse_slash('/title "Research Notes"')
    assert help_command is not None and title_command is not None
    unchanged, should_exit = runner._slash(conversation, help_command)
    updated, _ = runner._slash(unchanged, title_command)

    assert should_exit is False
    assert updated["title"] == "Research Notes"
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


def test_active_project_input_requires_an_explicit_intervention_choice(tmp_path: Path) -> None:
    client = FakeProjectChatClient()
    answers = iter(["1", "2", ""])
    runner = ChatRunner(
        client,  # type: ignore[arg-type]
        Output(json_mode=False, no_color=True, stdout=StringIO(), stderr=StringIO()),
        history_path=tmp_path / "history",
        selection_prompt=lambda _message: next(answers),
    )
    conversation = {
        **_conversation("conversation-1"),
        "_cli_mode": "project",
        "_cli_project_id": "project-1",
        "_cli_project_session_id": "session-1",
    }

    assert runner._dispatch_project_input(conversation, "local direction") is True
    assert runner._dispatch_project_input(conversation, "change scope") is True
    assert runner._dispatch_project_input(conversation, "do nothing") is True

    assert client.guidance[0]["content"] == "local direction"
    assert client.escalations[0]["content"] == "change scope"
    assert len(client.guidance) == len(client.escalations) == 1
