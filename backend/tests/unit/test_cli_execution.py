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

    def stream_run_events(self, _run_id: str, *, after_sequence: int = 0):
        self.cursor = after_sequence
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


def test_watch_cursor_and_ctrl_c_only_detach_viewer() -> None:
    client = FakeExecutionClient(interrupt=True)
    result = RunWatcher(client, _json_output()).watch("run-1", after_sequence=7)  # type: ignore[arg-type]

    assert client.cursor == 7
    assert result["interrupted"] is True
    assert result["run"]["status"] == "completed"


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
