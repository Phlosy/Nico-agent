"""Scrolling prompt_toolkit chat loop backed entirely by Nico HTTP/SSE APIs."""

from __future__ import annotations

import mimetypes
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from prompt_toolkit import PromptSession, prompt
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings

from nico_agent.cli.client import NicoApiClient
from nico_agent.cli.errors import CliError
from nico_agent.cli.execution import RunWatcher, write_binary_result
from nico_agent.cli.output import Output
from nico_agent.cli.renderers import ExecutionRenderer
from nico_agent.cli.slash import COMMANDS, SlashCommand, help_rows, parse_slash

_TERMINAL = {"completed", "failed", "cancelled", "timed_out"}


class ChatRunner:
    def __init__(
        self,
        client: NicoApiClient,
        output: Output,
        *,
        history_path: Path,
        approval_prompt: Callable[[str], str] | None = None,
    ) -> None:
        self.client = client
        self.output = output
        self.history_path = history_path
        self.renderer = ExecutionRenderer(output)
        self.approval_prompt = approval_prompt or prompt

    def resolve(
        self,
        *,
        project_id: str | None,
        agent_id: str | None,
        agent_version_id: str | None,
        resume_id: str | None,
        continue_latest: bool,
        title: str,
    ) -> dict[str, Any]:
        if resume_id is not None and continue_latest:
            raise CliError(
                "CHAT_SELECTION_CONFLICT",
                "--resume and --continue cannot be used together",
                exit_code=2,
            )
        if resume_id is not None:
            return self.client.get_conversation(resume_id)
        if continue_latest:
            values = self.client.list_conversations(
                project_id=project_id,
                agent_id=agent_id,
                status="active",
                limit=1,
            )
            if not values:
                raise CliError(
                    "CONVERSATION_NOT_FOUND",
                    "no active conversation matches --continue",
                    exit_code=2,
                )
            return values[0]
        if project_id is None or agent_id is None:
            raise CliError(
                "CHAT_TARGET_REQUIRED",
                "new chat requires --project and --agent",
                exit_code=2,
            )
        return self.client.create_conversation(
            project_id=project_id,
            agent_id=agent_id,
            agent_version_id=agent_version_id,
            title=title,
            idempotency_key=str(uuid4()),
        )

    def history(self, conversation_id: str) -> list[dict[str, Any]]:
        return self.client.list_conversation_turns(conversation_id, limit=500)

    def submit(
        self,
        conversation: dict[str, Any],
        message: str,
    ) -> dict[str, Any]:
        if not message.strip():
            raise CliError("EMPTY_CHAT_MESSAGE", "chat message cannot be empty", exit_code=2)
        turn = self.client.create_conversation_turn(
            conversation["id"],
            message,
            idempotency_key=str(uuid4()),
        )
        events: list[dict[str, Any]] = []
        events_truncated = False
        pending_approval: dict[str, Any] | None = None
        try:
            for event in self.client.stream_run_events(turn["run_id"]):
                if self.output.json_mode:
                    if len(events) < 2_000:
                        events.append(event)
                    else:
                        events_truncated = True
                else:
                    self.renderer.event(event)
                if event.get("type") == "ApprovalRequested":
                    payload = event.get("payload") or {}
                    approval_id = payload.get("approval_id")
                    if approval_id:
                        pending_approval = self.client.get_tool_approval(str(approval_id))
                        if not self.output.json_mode:
                            self.renderer.approval(pending_approval)
                        if self._can_prompt_for_approval():
                            if self._decide_interactively(pending_approval):
                                pending_approval = None
                                continue
                        break
        except KeyboardInterrupt:
            current = self.client.get_conversation_turn(turn["id"])
            if current["run_status"] not in _TERMINAL:
                try:
                    current = self.client.cancel_conversation_turn(
                        turn["id"],
                        expected_run_revision=current["run_revision"],
                    )
                except CliError as exc:
                    if exc.code not in {
                        "REVISION_CONFLICT",
                        "INVALID_STATE_TRANSITION",
                        "RUN_TERMINAL",
                    }:
                        raise
                    current = self.client.get_conversation_turn(turn["id"])
            if not self.output.json_mode:
                self.output.emit(current, title="Run cancelled")
            return {
                "conversation": conversation,
                "turn": current,
                "events": events,
                "events_truncated": events_truncated,
                "approval_required": pending_approval,
            }
        final = self.client.get_conversation_turn(turn["id"])
        result = {
            "conversation": conversation,
            "turn": final,
            "events": events,
            "events_truncated": events_truncated,
            "approval_required": pending_approval,
        }
        if not self.output.json_mode:
            self.renderer.final(final)
        return result

    def run_interactive(self, conversation: dict[str, Any], *, read_only: bool) -> None:
        if read_only:
            self.output.emit(
                {
                    "conversation_id": conversation["id"],
                    "title": conversation["title"],
                    "agent_version_id": conversation["agent_version_id"],
                    "read_only": True,
                },
                title="Nico Chat",
            )
        else:
            self.renderer.header(self._metadata(conversation))
        if read_only:
            self.output.table(
                self.history(conversation["id"]),
                title="Conversation History",
                columns=["sequence", "status", "user_input", "assistant_output"],
            )
            return
        if not sys.stdin.isatty():
            raise CliError(
                "INTERACTIVE_TTY_REQUIRED",
                "interactive chat requires a terminal; pass MESSAGE for one-shot chat",
                exit_code=2,
            )
        self._resume_pending_approval(conversation)
        session = PromptSession(
            history=FileHistory(str(self._secure_history_file())),
            key_bindings=_key_bindings(),
            completer=WordCompleter([f"/{name}" for name in COMMANDS], sentence=True),
            multiline=False,
        )
        current = conversation
        while True:
            try:
                message = session.prompt("you › ")
            except KeyboardInterrupt:
                continue
            except EOFError:
                break
            try:
                command = parse_slash(message)
                if command is not None:
                    current, should_exit = self._slash(current, command)
                    if should_exit:
                        break
                    continue
                self.submit(current, message)
            except CliError as exc:
                self.output.error(exc)

    def _slash(
        self,
        conversation: dict[str, Any],
        command: SlashCommand,
    ) -> tuple[dict[str, Any], bool]:
        name = command.name
        if name == "exit":
            return conversation, True
        if name == "help":
            self.output.table(
                help_rows(),
                title="Nico Slash Commands",
                columns=["group", "command", "description"],
            )
            return conversation, False
        if name == "history":
            self.output.table(
                self.history(conversation["id"]),
                title="Conversation History",
                columns=["sequence", "status", "user_input", "assistant_output"],
            )
            return conversation, False
        if name == "conversations":
            rows = self.client.list_conversations(status="active", limit=50)
            self.output.table(
                rows,
                title="Conversations",
                columns=["id", "title", "agent_id", "last_turn_id", "updated_at"],
            )
            return conversation, False
        if name == "resume":
            self._arity(command, 1, "/resume ID")
            selected = self.client.get_conversation(command.args[0])
            self.renderer.header(self._metadata(selected))
            return selected, False
        if name == "continue":
            self._arity(command, 0, "/continue")
            rows = self.client.list_conversations(
                project_id=conversation.get("project_id"),
                agent_id=conversation.get("agent_id"),
                status="active",
                limit=1,
            )
            if not rows:
                raise CliError("CONVERSATION_NOT_FOUND", "no active conversation found")
            self.renderer.header(self._metadata(rows[0]))
            return rows[0], False
        if name == "new":
            self._arity(command, 0, "/new")
            selected = self.client.create_conversation(
                project_id=conversation["project_id"],
                agent_id=conversation["agent_id"],
                agent_version_id=conversation["agent_version_id"],
                title="New conversation",
                idempotency_key=str(uuid4()),
            )
            self.renderer.header(self._metadata(selected))
            return selected, False
        if name == "title":
            if not command.args:
                raise CliError("SLASH_ARGUMENT_REQUIRED", "usage: /title TEXT", exit_code=2)
            new_title = " ".join(command.args)
            current = self.client.get_conversation(conversation["id"])
            selected = self.client.update_conversation(
                conversation["id"],
                expected_revision=current["revision"],
                title=new_title,
            )
            self.output.emit({"id": selected["id"], "title": selected["title"]}, title="Title")
            return selected, False
        if name == "attach":
            self._arity(command, 1, "/attach PATH")
            path = Path(command.args[0]).expanduser()
            try:
                if not path.is_file():
                    raise OSError("not a regular file")
                data = path.read_bytes()
            except OSError as exc:
                raise CliError(
                    "ATTACHMENT_UNREADABLE", f"cannot read attachment: {path}", exit_code=2
                ) from exc
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            value = self.client.upload_conversation_attachment(
                conversation["id"],
                name=path.name,
                content_type=content_type,
                idempotency_key=str(uuid4()),
                data=data,
            )
            self.output.emit(value, title="Attachment staged for the next Turn")
            return conversation, False
        if name == "compact":
            self._arity(command, 0, "/compact")
            accepted = self.client.compact_conversation(
                conversation["id"], idempotency_key=str(uuid4())
            )
            watched = RunWatcher(self.client, self.output).watch(accepted["run_id"])
            selected = self.client.get_conversation(conversation["id"])
            if not watched["interrupted"]:
                self.output.emit(
                    {
                        "conversation_id": selected["id"],
                        "summary_through_sequence": selected["summary_through_sequence"],
                        "summary_input_hash": selected["summary_input_hash"],
                        "summary_model_call_id": selected["summary_model_call_id"],
                        "summary": selected["summary"],
                    },
                    title="Conversation compacted",
                )
            return selected, False
        if name == "download":
            if len(command.args) not in {1, 2}:
                raise CliError(
                    "INVALID_SLASH_ARGUMENTS",
                    "usage: /download ARTIFACT_ID [PATH]",
                    exit_code=2,
                )
            artifact_id = command.args[0]
            found: tuple[str, dict[str, Any]] | None = None
            for historical_turn in reversed(self.history(conversation["id"])):
                for reference in historical_turn.get("artifact_refs") or []:
                    if (
                        isinstance(reference, dict)
                        and str(reference.get("artifact_id")) == artifact_id
                    ):
                        found = (
                            str(reference.get("owner_run_id") or historical_turn["run_id"]),
                            reference,
                        )
                        break
                if found:
                    break
            if found is None:
                raise CliError(
                    "ARTIFACT_NOT_IN_CONVERSATION",
                    "the Artifact is not referenced by this conversation",
                )
            run_id, reference = found
            destination = Path(command.args[1] if len(command.args) == 2 else reference["name"])
            data, content_type = self.client.download_artifact(run_id, artifact_id)
            write_binary_result(destination, data)
            self.output.emit(
                {
                    "artifact_id": artifact_id,
                    "path": str(destination),
                    "content_type": content_type,
                    "size_bytes": len(data),
                },
                title="Artifact downloaded",
            )
            return conversation, False

        turn = self._latest_turn(conversation)
        run_id = turn.get("run_id") if turn else None
        if name == "status":
            self.output.emit({"conversation": conversation, "turn": turn}, title="Status")
        elif name == "agent":
            self.output.emit(self.client.get_agent(conversation["agent_id"]), title="Agent")
        elif name == "version":
            versions = self.client.list_agent_versions(conversation["agent_id"])
            value = next(
                (item for item in versions if item.get("id") == conversation["agent_version_id"]),
                {"id": conversation["agent_version_id"]},
            )
            self.output.emit(value, title="Agent Version")
        elif name == "cancel":
            self._require_turn(turn)
            if turn["run_status"] in _TERMINAL:
                raise CliError("RUN_TERMINAL", "the latest Turn is already terminal")
            value = self.client.cancel_conversation_turn(
                turn["id"], expected_run_revision=turn["run_revision"]
            )
            self.output.emit(value, title="Cancelled")
        elif name == "retry":
            self._require_turn(turn)
            accepted = self.client.retry_conversation_turn(
                turn["id"],
                expected_run_id=turn["run_id"],
                expected_run_revision=turn["run_revision"],
            )
            for event in self.client.stream_run_events(accepted["run_id"]):
                self.renderer.event(event)
            final = self.client.get_conversation_turn(accepted["id"])
            self.renderer.final(final)
        elif name == "approvals":
            self._require_run(run_id)
            self.renderer.approvals(self.client.list_tool_approvals(run_id=str(run_id)))
        elif name == "approve":
            if len(command.args) != 2 or command.args[1] not in {"once", "run"}:
                raise CliError(
                    "INVALID_SLASH_ARGUMENTS",
                    "usage: /approve ID once|run",
                    exit_code=2,
                )
            approval = self.client.get_tool_approval(command.args[0])
            value = self.client.decide_tool_approval(
                command.args[0],
                expected_revision=approval["revision"],
                decision="approve",
                allowed_scope=command.args[1],
                idempotency_key=str(uuid4()),
            )
            self.output.emit(value, title="Tool approval saved")
        elif name == "reject":
            if not command.args:
                raise CliError(
                    "INVALID_SLASH_ARGUMENTS",
                    "usage: /reject ID [REASON]",
                    exit_code=2,
                )
            approval = self.client.get_tool_approval(command.args[0])
            value = self.client.decide_tool_approval(
                command.args[0],
                expected_revision=approval["revision"],
                decision="reject",
                allowed_scope=None,
                reason=" ".join(command.args[1:]) or None,
                idempotency_key=str(uuid4()),
            )
            self.output.emit(value, title="Tool rejection saved")
        else:
            self._require_run(run_id)
            self._inspect(name, str(run_id), turn)
        return conversation, False

    def _inspect(self, name: str, run_id: str, turn: dict[str, Any] | None) -> None:
        run = self.client.get_run(run_id)
        if name == "runtime":
            self.output.emit(self.client.get_runtime(run_id), title="Runtime")
        elif name == "usage":
            self.renderer.usage(self.client.get_runtime(run_id), run)
        elif name == "context":
            self.output.table(self.client.list_run_contexts(run_id), title="Context Snapshots")
        elif name == "plan":
            plans = self.client.list_run_plans(run_id)
            self.renderer.plans(plans)
            if plans:
                self.renderer.plan_steps(self.client.list_plan_steps(run_id, plans[-1]["id"]))
        elif name == "steps":
            self.renderer.run_steps(self.client.list_run_steps(run_id))
        elif name == "tools":
            self.renderer.tools(self.client.list_run_tool_calls(run_id))
        elif name == "approvals":
            self.renderer.approvals(self.client.list_tool_approvals(run_id=run_id))
        elif name == "children":
            self.output.table(self.client.list_run_children(run_id), title="Child Runs")
        elif name == "messages":
            self.output.table(self.client.list_run_messages(run_id), title="Agent Messages")
        elif name == "artifacts":
            self.renderer.artifacts(self.client.list_run_artifacts(run_id))
        elif name == "audit":
            identifiers = {run_id, str(run.get("task_id")), str((turn or {}).get("id"))}
            rows = [
                item
                for item in self.client.list_audit(limit=500)
                if str(item.get("resource_id")) in identifiers
            ]
            self.output.table(
                rows,
                title="Audit",
                columns=["sequence", "action", "resource_type", "actor_id", "created_at"],
            )
        elif name == "inspect":
            self.output.emit(run, title="Run")
            self._inspect("plan", run_id, turn)
            self._inspect("steps", run_id, turn)
            self._inspect("tools", run_id, turn)
            self._inspect("artifacts", run_id, turn)
            self._inspect("usage", run_id, turn)
        else:
            raise CliError("UNKNOWN_SLASH_COMMAND", f"unknown command '/{name}'")

    def _metadata(self, conversation: dict[str, Any]) -> dict[str, Any]:
        project = self.client.get_project(conversation["project_id"])
        agent = self.client.get_agent(conversation["agent_id"])
        versions = self.client.list_agent_versions(conversation["agent_id"])
        version = next(
            (item for item in versions if item.get("id") == conversation["agent_version_id"]),
            {},
        )
        policy = version.get("tool_policy") or {}
        return {
            "project": project.get("name") or project.get("id"),
            "agent": agent.get("display_name") or agent.get("name") or agent.get("id"),
            "version": version.get("version") or conversation.get("agent_version_id"),
            "runtime": (
                version.get("runtime_provider") or version.get("execution_mode") or "default"
            ),
            "tools": policy.get("allow") or policy.get("tools") or [],
        }

    def _latest_turn(self, conversation: dict[str, Any]) -> dict[str, Any] | None:
        rows = self.client.list_conversation_turns(conversation["id"], limit=500)
        return rows[-1] if rows else None

    def _can_prompt_for_approval(self) -> bool:
        return not self.output.json_mode and sys.stdin.isatty()

    def _decide_interactively(self, approval: dict[str, Any]) -> bool:
        if approval.get("status") != "requested":
            self.output.emit(approval, title="Tool approval already decided")
            return True
        while True:
            try:
                choice = self.approval_prompt("allow › ").strip()
            except (EOFError, KeyboardInterrupt):
                self.output.out.print(
                    "[yellow]审批仍保存在服务端；可稍后使用 /approvals 恢复。[/yellow]"
                )
                return False
            if choice in {"1", "2", "3"}:
                break
            self.output.out.print("[yellow]请输入 1、2 或 3。[/yellow]")
        approved = choice in {"1", "2"}
        value = self.client.decide_tool_approval(
            str(approval["id"]),
            expected_revision=int(approval["revision"]),
            decision="approve" if approved else "reject",
            allowed_scope={"1": "once", "2": "run", "3": None}[choice],
            reason=None if approved else "rejected from Nico CLI",
            idempotency_key=str(uuid4()),
        )
        self.output.emit(value, title="Tool approval saved")
        return True

    def _resume_pending_approval(self, conversation: dict[str, Any]) -> None:
        turn = self._latest_turn(conversation)
        if turn is None or not turn.get("run_id"):
            return
        approvals = self.client.list_tool_approvals(run_id=str(turn["run_id"]), status="requested")
        events = self.client.list_run_events(str(turn["run_id"])) if approvals else []
        after_sequence = max((int(event.get("sequence") or 0) for event in events), default=0)
        for approval in approvals:
            self.renderer.approval(approval)
            if not self._decide_interactively(approval):
                return
        if approvals:
            for event in self.client.stream_run_events(
                str(turn["run_id"]), after_sequence=after_sequence
            ):
                self.renderer.event(event)
                if event.get("type") == "ApprovalRequested":
                    payload = event.get("payload") or {}
                    approval_id = payload.get("approval_id")
                    if approval_id:
                        next_approval = self.client.get_tool_approval(str(approval_id))
                        self.renderer.approval(next_approval)
                        if not self._decide_interactively(next_approval):
                            return
            self.renderer.final(self.client.get_conversation_turn(turn["id"]))

    @staticmethod
    def _arity(command: SlashCommand, expected: int, usage: str) -> None:
        if len(command.args) != expected:
            raise CliError("INVALID_SLASH_ARGUMENTS", f"usage: {usage}", exit_code=2)

    @staticmethod
    def _require_turn(turn: dict[str, Any] | None) -> None:
        if turn is None:
            raise CliError("CONVERSATION_EMPTY", "the conversation has no Turn")

    @staticmethod
    def _require_run(run_id: Any) -> None:
        if not run_id:
            raise CliError("CONVERSATION_EMPTY", "the conversation has no Run")

    def _secure_history_file(self) -> Path:
        self.history_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.history_path.parent, 0o700)
        descriptor = os.open(self.history_path, os.O_CREAT | os.O_APPEND, 0o600)
        os.close(descriptor)
        os.chmod(self.history_path, 0o600)
        return self.history_path


def _key_bindings() -> KeyBindings:
    bindings = KeyBindings()

    @bindings.add("escape", "enter")
    def insert_newline(event) -> None:
        event.current_buffer.insert_text("\n")

    return bindings
