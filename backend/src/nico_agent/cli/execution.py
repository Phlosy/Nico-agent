"""Reusable one-shot execution and detached Run watching."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from nico_agent.cli.client import NicoApiClient
from nico_agent.cli.errors import CliError
from nico_agent.cli.output import Output
from nico_agent.cli.renderers import ExecutionRenderer

TERMINAL_RUN_STATES = {"completed", "failed", "cancelled", "timed_out"}
_FINAL_FETCH_RESULT = "_final_fetch_result"


class RunWatcher:
    def __init__(self, client: NicoApiClient, output: Output) -> None:
        self.client = client
        self.output = output
        self.renderer = ExecutionRenderer(output)

    def watch(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        _final_fetch: Callable[[], Any] | None = None,
    ) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        events_truncated = False
        interrupted = False
        approval_required: dict[str, Any] | None = None
        final_fetch_result: Any = None
        final_fetch_completed = False
        progress = self.renderer.progress()
        stream_options = {} if self.output.json_mode else {"on_connection": progress.connection}
        run: dict[str, Any] | None = None
        try:
            with progress:
                for event in self.client.stream_run_events(
                    run_id,
                    after_sequence=after_sequence,
                    **stream_options,
                ):
                    if self.output.json_mode:
                        if len(events) < 2_000:
                            events.append(event)
                        else:
                            events_truncated = True
                    else:
                        progress.event(event)
                    if event.get("type") == "ApprovalRequested":
                        payload = event.get("payload") or {}
                        approval_id = payload.get("approval_id")
                        if approval_id:
                            approval_required = self.client.get_tool_approval(str(approval_id))
                            if not self.output.json_mode:
                                progress.pause()
                                self.renderer.approval(approval_required)
                        break
                run = self.client.get_run(run_id)
                if _final_fetch is not None:
                    final_fetch_result = _final_fetch()
                    final_fetch_completed = True
        except KeyboardInterrupt:
            # Watching is observational: Ctrl+C detaches and never cancels the Run.
            interrupted = True
            if not self.output.json_mode:
                self.output.out.print("[yellow]已停止监看；Run 仍在服务端继续执行。[/yellow]")
        if run is None:
            run = self.client.get_run(run_id)
        result = {
            "run": run,
            "events": events,
            "events_truncated": events_truncated,
            "interrupted": interrupted,
            "approval_required": approval_required,
        }
        if final_fetch_completed:
            result[_FINAL_FETCH_RESULT] = final_fetch_result
        return result


class ExecRunner:
    def __init__(self, client: NicoApiClient, output: Output) -> None:
        self.client = client
        self.output = output
        self.renderer = ExecutionRenderer(output)
        self.watcher = RunWatcher(client, output)

    def execute(
        self,
        *,
        prompt: str,
        project_id: str,
        agent_id: str,
        agent_version_id: str | None,
        title: str,
        detach: bool,
    ) -> dict[str, Any]:
        if not prompt.strip():
            raise CliError("EMPTY_EXEC_PROMPT", "exec prompt cannot be empty", exit_code=2)
        conversation = self.client.create_conversation(
            project_id=project_id,
            agent_id=agent_id,
            agent_version_id=agent_version_id,
            title=title,
            idempotency_key=str(uuid4()),
        )
        if not self.output.json_mode:
            self.renderer.header(self.metadata(conversation))
        turn = self.client.create_conversation_turn(
            conversation["id"],
            prompt,
            idempotency_key=str(uuid4()),
        )
        identifiers = {
            "conversation_id": conversation["id"],
            "turn_id": turn["id"],
            "task_id": turn["task_id"],
            "run_id": turn["run_id"],
        }
        if detach:
            result = {"detached": True, **identifiers, "conversation": conversation, "turn": turn}
            if not self.output.json_mode:
                self.output.emit(identifiers, title="Run 已在后台启动")
            return result
        watched = self.watcher.watch(
            turn["run_id"],
            _final_fetch=lambda: self.client.get_conversation_turn(turn["id"]),
        )
        if _FINAL_FETCH_RESULT in watched:
            final = watched.pop(_FINAL_FETCH_RESULT)
        else:
            # Ctrl+C exits the progress scope before preserving the existing final-Turn fetch.
            final = self.client.get_conversation_turn(turn["id"])
        result = {
            "detached": False,
            **identifiers,
            "conversation": conversation,
            "turn": final,
            "run": watched["run"],
            "events": watched["events"],
            "events_truncated": watched["events_truncated"],
            "interrupted": watched["interrupted"],
            "approval_required": watched["approval_required"],
        }
        if not watched["interrupted"] and watched["approval_required"] is None:
            self.renderer.final(final)
        return result

    def metadata(self, conversation: dict[str, Any]) -> dict[str, Any]:
        project = self.client.get_project(conversation["project_id"])
        agent = self.client.get_agent(conversation["agent_id"])
        versions = self.client.list_agent_versions(conversation["agent_id"])
        version = next(
            (value for value in versions if value.get("id") == conversation["agent_version_id"]),
            {},
        )
        tool_policy = version.get("tool_policy") or {}
        allowed = tool_policy.get("allow") or tool_policy.get("tools") or []
        runtime = version.get("runtime_provider") or version.get("execution_mode") or "default"
        return {
            "project": project.get("name") or project.get("id"),
            "agent": agent.get("display_name") or agent.get("name") or agent.get("id"),
            "version": version.get("version") or conversation.get("agent_version_id"),
            "runtime": runtime,
            "tools": allowed,
        }


def load_exec_input(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CliError(
            "EXEC_INPUT_UNREADABLE", f"cannot read input file: {path}", exit_code=2
        ) from exc
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise CliError(
            "INVALID_EXEC_INPUT", "--input must contain one JSON object", exit_code=2
        ) from exc
    if not isinstance(value, dict):
        raise CliError("INVALID_EXEC_INPUT", "--input must contain one JSON object", exit_code=2)
    unknown = set(value) - {"prompt", "project_id", "agent_id", "agent_version_id", "title"}
    if unknown:
        raise CliError(
            "INVALID_EXEC_INPUT",
            f"unsupported --input fields: {', '.join(sorted(unknown))}",
            exit_code=2,
        )
    return value


def write_result(path: Path, value: dict[str, Any]) -> None:
    """Atomically persist a private JSON result without following a final symlink."""

    temporary: str | None = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.exists() and path.is_symlink():
            raise CliError("UNSAFE_OUTPUT_PATH", "--output cannot be a symbolic link", exit_code=2)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except CliError:
        raise
    except OSError as exc:
        raise CliError(
            "EXEC_OUTPUT_UNWRITABLE",
            f"cannot write output file: {path}",
            exit_code=2,
        ) from exc
    except Exception:
        raise
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def write_binary_result(path: Path, data: bytes) -> None:
    """Atomically write private downloaded bytes without following a final symlink."""

    temporary: str | None = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.exists() and path.is_symlink():
            raise CliError("UNSAFE_OUTPUT_PATH", "download target cannot be a symbolic link")
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except CliError:
        raise
    except OSError as exc:
        raise CliError(
            "DOWNLOAD_OUTPUT_UNWRITABLE", f"cannot write download target: {path}", exit_code=2
        ) from exc
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
