from __future__ import annotations

import json
import stat
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from nico_agent.cli.errors import CliError
from nico_agent.cli.execution import (
    ExecRunner,
    RunWatcher,
    load_exec_input,
    write_binary_result,
    write_result,
)
from nico_agent.cli.output import Output


class FakeExecutionClient:
    def __init__(self, *, interrupt: bool = False) -> None:
        self.interrupt = interrupt
        self.cursor: int | None = None

    def create_conversation(self, **_kwargs) -> dict[str, Any]:
        return {
            "id": "conversation-1",
            "project_id": "project-1",
            "agent_id": "agent-1",
            "agent_version_id": "version-1",
            "title": "Exec",
        }

    def create_conversation_turn(self, *_args, **_kwargs) -> dict[str, Any]:
        return {"id": "turn-1", "task_id": "task-1", "run_id": "run-1"}

    def stream_run_events(
        self,
        _run_id: str,
        *,
        after_sequence: int = 0,
        on_connection=None,
    ):
        self.cursor = after_sequence
        if on_connection is not None:
            on_connection("reconnecting", 1, 3)
            on_connection("recovered", 1, 3)
        yield {"sequence": after_sequence + 1, "type": "RunStarted", "payload": {}}
        if self.interrupt:
            raise KeyboardInterrupt
        yield {"sequence": after_sequence + 2, "type": "RunCompleted", "payload": {}}

    def get_run(self, run_id: str) -> dict[str, Any]:
        return {"id": run_id, "task_id": "task-1", "status": "completed", "cost": {}}

    def get_conversation_turn(self, turn_id: str) -> dict[str, Any]:
        return {
            "id": turn_id,
            "run_id": "run-1",
            "status": "completed",
            "assistant_output": {"answer": "done"},
            "error": None,
        }

    def get_project(self, _value: str) -> dict[str, Any]:
        return {"id": "project-1", "name": "Project"}

    def get_agent(self, _value: str) -> dict[str, Any]:
        return {"id": "agent-1", "display_name": "Agent"}

    def list_agent_versions(self, _value: str) -> list[dict[str, Any]]:
        return [{"id": "version-1", "version": 1, "runtime_provider": "mock"}]


class FakeApprovalExecutionClient(FakeExecutionClient):
    def __init__(self) -> None:
        super().__init__()
        self.connection_observer = None

    def stream_run_events(
        self,
        _run_id: str,
        *,
        after_sequence: int = 0,
        on_connection=None,
    ):
        self.connection_observer = on_connection
        yield {"sequence": after_sequence + 1, "type": "RunStarted", "payload": {}}
        yield {
            "sequence": after_sequence + 2,
            "type": "ApprovalRequested",
            "payload": {"approval_id": "approval-1"},
        }

    def get_run(self, run_id: str) -> dict[str, Any]:
        return {
            "id": run_id,
            "task_id": "task-1",
            "status": "waiting_for_approval",
            "cost": {},
        }

    def get_tool_approval(self, approval_id: str) -> dict[str, Any]:
        return {
            "id": approval_id,
            "tool_name": "private_tool",
            "tool_version": "1",
            "risk_level": "high",
            "arguments": {"query": "safe summary"},
        }

    def get_conversation_turn(self, turn_id: str) -> dict[str, Any]:
        return {
            "id": turn_id,
            "run_id": "run-1",
            "status": "waiting_for_approval",
            "assistant_output": None,
            "error": None,
            "internal_state": "pending-secret",
        }


class RecordingProgress:
    def __init__(self, actions: list[str]) -> None:
        self.actions = actions

    def __enter__(self):
        self.actions.append("progress:start")
        return self

    def __exit__(self, *_args) -> None:
        self.actions.append("progress:stop")

    def event(self, event: dict[str, Any]) -> None:
        self.actions.append(f"progress:event:{event['type']}")

    def connection(self, state: str, _attempt=None, _maximum=None) -> None:
        self.actions.append(f"progress:connection:{state}")

    def pause(self) -> None:
        self.actions.append("progress:pause")


def _json_output() -> Output:
    return Output(json_mode=True, no_color=True, stdout=StringIO(), stderr=StringIO())


def test_exec_attached_and_detached_return_stable_identifiers() -> None:
    client = FakeExecutionClient()
    runner = ExecRunner(client, _json_output())  # type: ignore[arg-type]

    detached = runner.execute(
        prompt="research",
        project_id="project-1",
        agent_id="agent-1",
        agent_version_id=None,
        title="Exec",
        detach=True,
    )
    attached = runner.execute(
        prompt="research",
        project_id="project-1",
        agent_id="agent-1",
        agent_version_id=None,
        title="Exec",
        detach=False,
    )

    assert detached["detached"] is True
    assert detached["run_id"] == "run-1"
    assert attached["turn"]["assistant_output"] == {"answer": "done"}
    assert [event["sequence"] for event in attached["events"]] == [1, 2]


def test_detached_human_exec_never_starts_foreground_progress(monkeypatch) -> None:
    client = FakeExecutionClient()
    output = Output(json_mode=False, no_color=True, stdout=StringIO(), stderr=StringIO())
    runner = ExecRunner(client, output)  # type: ignore[arg-type]
    monkeypatch.setattr(
        runner.watcher,
        "watch",
        lambda _run_id: (_ for _ in ()).throw(AssertionError("foreground watcher started")),
    )

    result = runner.execute(
        prompt="research",
        project_id="project-1",
        agent_id="agent-1",
        agent_version_id=None,
        title="Exec",
        detach=True,
    )

    assert result["detached"] is True
    assert "Run 已在后台启动" in output.stdout.getvalue()


def test_watch_cursor_and_ctrl_c_only_detach_viewer() -> None:
    client = FakeExecutionClient(interrupt=True)
    result = RunWatcher(client, _json_output()).watch("run-1", after_sequence=7)  # type: ignore[arg-type]

    assert client.cursor == 7
    assert result["interrupted"] is True
    assert result["run"]["status"] == "completed"


def test_human_watch_clears_progress_before_detaching_on_ctrl_c(monkeypatch) -> None:
    client = FakeExecutionClient(interrupt=True)
    output = Output(json_mode=False, no_color=True, stdout=StringIO(), stderr=StringIO())
    watcher = RunWatcher(client, output)  # type: ignore[arg-type]
    actions: list[str] = []

    monkeypatch.setattr(watcher.renderer, "progress", lambda: RecordingProgress(actions))
    original_get_run = client.get_run

    def get_run(run_id: str) -> dict[str, Any]:
        actions.append("client:get-run")
        return original_get_run(run_id)

    monkeypatch.setattr(client, "get_run", get_run)

    result = watcher.watch("run-1")

    assert result["interrupted"] is True
    assert actions.index("progress:stop") < actions.index("client:get-run")
    assert "已停止监看" in output.stdout.getvalue()


def test_human_watch_pauses_progress_before_approval_panel(monkeypatch) -> None:
    client = FakeApprovalExecutionClient()
    output = Output(json_mode=False, no_color=True, stdout=StringIO(), stderr=StringIO())
    watcher = RunWatcher(client, output)  # type: ignore[arg-type]
    actions: list[str] = []
    monkeypatch.setattr(watcher.renderer, "progress", lambda: RecordingProgress(actions))
    monkeypatch.setattr(
        watcher.renderer,
        "approval",
        lambda _approval: actions.append("render:approval"),
    )

    result = watcher.watch("run-1")

    assert result["approval_required"]["id"] == "approval-1"
    assert actions.index("progress:pause") < actions.index("render:approval")


def test_json_watch_buffers_approval_event_without_human_observer(monkeypatch) -> None:
    client = FakeApprovalExecutionClient()
    watcher = RunWatcher(client, _json_output())  # type: ignore[arg-type]
    monkeypatch.setattr(
        watcher.renderer,
        "approval",
        lambda _approval: (_ for _ in ()).throw(AssertionError("human approval rendered")),
    )

    result = watcher.watch("run-1")

    assert [event["type"] for event in result["events"]] == [
        "RunStarted",
        "ApprovalRequested",
    ]
    assert result["approval_required"]["id"] == "approval-1"
    assert client.connection_observer is None


@pytest.mark.parametrize("failure_source", ["get_run", "final_fetch"])
def test_watch_stops_progress_before_fetch_error_propagates(monkeypatch, failure_source) -> None:
    client = FakeExecutionClient()
    output = Output(json_mode=False, no_color=True, stdout=StringIO(), stderr=StringIO())
    watcher = RunWatcher(client, output)  # type: ignore[arg-type]
    actions: list[str] = []
    monkeypatch.setattr(watcher.renderer, "progress", lambda: RecordingProgress(actions))

    if failure_source == "get_run":
        monkeypatch.setattr(
            client,
            "get_run",
            lambda _run_id: (_ for _ in ()).throw(CliError("GET_RUN_FAILED", "get run failed")),
        )

        def final_fetch() -> None:
            return None

        expected_code = "GET_RUN_FAILED"
    else:

        def final_fetch() -> None:
            raise CliError("FINAL_FETCH_FAILED", "final fetch failed")

        expected_code = "FINAL_FETCH_FAILED"

    with pytest.raises(CliError) as captured:
        watcher.watch("run-1", _final_fetch=final_fetch)

    assert captured.value.code == expected_code
    assert actions[-1] == "progress:stop"


def test_attached_exec_fetches_final_turn_before_progress_stops(monkeypatch) -> None:
    client = FakeExecutionClient()
    output = Output(json_mode=False, no_color=True, stdout=StringIO(), stderr=StringIO())
    runner = ExecRunner(client, output)  # type: ignore[arg-type]
    actions: list[str] = []
    monkeypatch.setattr(runner.watcher.renderer, "progress", lambda: RecordingProgress(actions))
    original_get_turn = client.get_conversation_turn

    def get_turn(turn_id: str) -> dict[str, Any]:
        actions.append("client:get-turn")
        return original_get_turn(turn_id)

    monkeypatch.setattr(client, "get_conversation_turn", get_turn)
    monkeypatch.setattr(runner.renderer, "final", lambda _turn: actions.append("render:final"))

    result = runner.execute(
        prompt="research",
        project_id="project-1",
        agent_id="agent-1",
        agent_version_id=None,
        title="Exec",
        detach=False,
    )

    assert actions.index("client:get-turn") < actions.index("progress:stop")
    assert actions.index("progress:stop") < actions.index("render:final")
    assert "_final_fetch_result" not in result


def test_attached_exec_fetches_turn_after_clean_interrupt(monkeypatch) -> None:
    client = FakeExecutionClient(interrupt=True)
    output = Output(json_mode=False, no_color=True, stdout=StringIO(), stderr=StringIO())
    runner = ExecRunner(client, output)  # type: ignore[arg-type]
    actions: list[str] = []
    monkeypatch.setattr(runner.watcher.renderer, "progress", lambda: RecordingProgress(actions))
    original_get_turn = client.get_conversation_turn

    def get_turn(turn_id: str) -> dict[str, Any]:
        actions.append("client:get-turn")
        return original_get_turn(turn_id)

    monkeypatch.setattr(client, "get_conversation_turn", get_turn)

    result = runner.execute(
        prompt="research",
        project_id="project-1",
        agent_id="agent-1",
        agent_version_id=None,
        title="Exec",
        detach=False,
    )

    assert result["interrupted"] is True
    assert actions.index("progress:stop") < actions.index("client:get-turn")


def test_attached_exec_does_not_render_pending_turn_for_approval(monkeypatch) -> None:
    client = FakeApprovalExecutionClient()
    output = Output(json_mode=False, no_color=True, stdout=StringIO(), stderr=StringIO())
    runner = ExecRunner(client, output)  # type: ignore[arg-type]
    monkeypatch.setattr(
        runner.renderer,
        "final",
        lambda _turn: (_ for _ in ()).throw(AssertionError("pending turn rendered")),
    )

    result = runner.execute(
        prompt="research",
        project_id="project-1",
        agent_id="agent-1",
        agent_version_id=None,
        title="Exec",
        detach=False,
    )

    assert result["approval_required"]["id"] == "approval-1"
    assert result["turn"]["internal_state"] == "pending-secret"
    assert "pending-secret" not in output.stdout.getvalue()


def test_standalone_watch_result_shape_excludes_internal_fetch_key() -> None:
    result = RunWatcher(FakeExecutionClient(), _json_output()).watch("run-1")  # type: ignore[arg-type]

    assert set(result) == {
        "run",
        "events",
        "events_truncated",
        "interrupted",
        "approval_required",
    }


def test_exec_input_is_strict_and_output_is_atomic_private(tmp_path: Path) -> None:
    source = tmp_path / "task.json"
    source.write_text('{"prompt":"go","project_id":"p","agent_id":"a"}')
    assert load_exec_input(source)["prompt"] == "go"

    source.write_text('{"prompt":"go","secret":"unexpected"}')
    with pytest.raises(CliError) as captured:
        load_exec_input(source)
    assert captured.value.code == "INVALID_EXEC_INPUT"

    target = tmp_path / "nested" / "result.json"
    write_result(target, {"run_id": "run-1"})
    assert json.loads(target.read_text()) == {"run_id": "run-1"}
    assert stat.S_IMODE(target.stat().st_mode) == 0o600

    unsafe = tmp_path / "result-link.json"
    unsafe.symlink_to(target)
    with pytest.raises(CliError) as unsafe_error:
        write_result(unsafe, {})
    assert unsafe_error.value.code == "UNSAFE_OUTPUT_PATH"

    artifact = tmp_path / "downloads" / "report.bin"
    write_binary_result(artifact, b"private artifact")
    assert artifact.read_bytes() == b"private artifact"
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600

    artifact_link = tmp_path / "artifact-link.bin"
    artifact_link.symlink_to(artifact)
    with pytest.raises(CliError) as artifact_error:
        write_binary_result(artifact_link, b"replacement")
    assert artifact_error.value.code == "UNSAFE_OUTPUT_PATH"
