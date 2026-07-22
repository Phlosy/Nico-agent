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

from nico_agent.cli.approvals import ApprovalCoordinator
from nico_agent.cli.client import NicoApiClient
from nico_agent.cli.errors import CliError
from nico_agent.cli.execution import RunWatcher, write_binary_result
from nico_agent.cli.output import Output
from nico_agent.cli.renderers import ExecutionRenderer
from nico_agent.cli.slash import COMMANDS, SlashCommand, help_rows, parse_slash

_TERMINAL = {"completed", "failed", "cancelled", "timed_out"}
_APPROVAL_MODES = ("ask", "auto-medium", "auto-all")


class ChatRunner:
    def __init__(
        self,
        client: NicoApiClient,
        output: Output,
        *,
        history_path: Path,
        approval_prompt: Callable[[str], str] | None = None,
        selection_prompt: Callable[[str], str] | None = None,
    ) -> None:
        self.client = client
        self.output = output
        self.history_path = history_path
        self.renderer = ExecutionRenderer(output)
        self.approval_prompt = approval_prompt or prompt
        self.selection_prompt = selection_prompt or prompt

    def resolve(
        self,
        *,
        project_id: str | None,
        agent_id: str | None,
        agent_version_id: str | None,
        resume_id: str | None,
        continue_latest: bool,
        title: str,
        interactive: bool = False,
        recent_agent_id: str | None = None,
    ) -> dict[str, Any]:
        if resume_id is not None and continue_latest:
            raise CliError(
                "CHAT_SELECTION_CONFLICT",
                "--resume and --continue cannot be used together",
                exit_code=2,
            )
        if resume_id is not None:
            resumed = self.client.get_conversation(resume_id)
            return self._with_mode(resumed, resumed.get("mode", "project"))
        mode = "personal" if project_id is None else "project"
        if continue_latest:
            resolved_agent_id = agent_id
            if mode == "personal":
                resolved_agent_id = self._resolve_agent(
                    agent_id,
                    recent_agent_id=recent_agent_id,
                    interactive=interactive,
                )["id"]
            values = self.client.list_conversations(
                project_id=project_id,
                agent_id=resolved_agent_id,
                status="active",
                mode=mode,
                limit=1,
            )
            if not values:
                raise CliError(
                    "CONVERSATION_NOT_FOUND",
                    "no active conversation matches --continue",
                    exit_code=2,
                )
            return self._with_mode(values[0], mode)
        selected = self._resolve_agent(
            agent_id,
            recent_agent_id=recent_agent_id,
            interactive=interactive,
        )
        created = self.client.create_conversation(
            project_id=project_id,
            agent_id=selected["id"],
            mode=mode,
            agent_version_id=agent_version_id,
            title=title,
            idempotency_key=str(uuid4()),
        )
        return self._with_mode(created, mode)

    def _resolve_agent(
        self,
        reference: str | None,
        *,
        recent_agent_id: str | None,
        interactive: bool,
    ) -> dict[str, Any]:
        ready = [agent for agent in self.client.list_agents() if agent.get("status") == "ready"]
        if reference is not None:
            matches = [
                agent
                for agent in ready
                if str(agent.get("id")) == reference or agent.get("name") == reference
            ]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                candidates = ", ".join(
                    f"{agent.get('name')} ({agent.get('id')})" for agent in matches
                )
                raise CliError(
                    "AGENT_NAME_AMBIGUOUS",
                    f"Agent reference '{reference}' matches multiple ready Agents: {candidates}",
                    exit_code=2,
                )
            raise CliError(
                "AGENT_NOT_FOUND",
                f"no ready Agent matches '{reference}'; run 'nico agent list'",
                exit_code=2,
            )
        if recent_agent_id is not None:
            recent = next(
                (agent for agent in ready if str(agent.get("id")) == recent_agent_id),
                None,
            )
            if recent is not None:
                return recent
        if len(ready) == 1:
            return ready[0]
        if not ready:
            raise CliError(
                "READY_AGENT_REQUIRED",
                "no ready Agent is available; run 'nico setup' or configure a Provider",
                exit_code=2,
            )
        if not interactive:
            raise CliError(
                "CHAT_AGENT_REQUIRED",
                "multiple ready Agents are available; pass --agent NAME_OR_ID",
                exit_code=2,
            )
        choices = [{"selection": index, **agent} for index, agent in enumerate(ready, start=1)]
        self.output.table(
            choices,
            title="Choose an Agent",
            columns=["selection", "name", "display_name", "status", "id"],
        )
        answer = self.selection_prompt("Agent number, exact name, or ID: ").strip()
        if not answer:
            raise CliError("CHAT_SELECTION_CANCELLED", "Agent selection was cancelled", exit_code=2)
        if answer.isdecimal():
            number = int(answer)
            if 1 <= number <= len(ready):
                return ready[number - 1]
        return self._resolve_agent(answer, recent_agent_id=None, interactive=False)

    @staticmethod
    def _with_mode(conversation: dict[str, Any], mode: str) -> dict[str, Any]:
        return {**conversation, "_cli_mode": mode}

    @staticmethod
    def public_conversation(conversation: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in conversation.items() if not key.startswith("_cli_")}

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
        progress = self.renderer.progress()
        stream_options = {} if self.output.json_mode else {"on_connection": progress.connection}
        try:
            with progress:
                for event in self.client.stream_run_events(turn["run_id"], **stream_options):
                    if self.output.json_mode:
                        if len(events) < 2_000:
                            events.append(event)
                        else:
                            events_truncated = True
                    else:
                        progress.event(event)
                    should_stop, approval = self._handle_stream_approval(event, progress)
                    if should_stop:
                        pending_approval = approval
                        break
                final = self.client.get_conversation_turn(turn["id"])
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
                event_type = {
                    "failed": "RunFailed",
                    "cancelled": "RunCancelled",
                    "timed_out": "RunTimedOut",
                }.get(str(current.get("run_status") or current.get("status") or ""))
                if event_type is not None:
                    self.renderer.event({"type": event_type, "payload": {}})
                elif current.get("assistant_output") is not None:
                    self.renderer.final(current)
                else:
                    self.output.out.print("[yellow]■ Run interrupted[/yellow]")
            return {
                "conversation": self.public_conversation(conversation),
                "turn": current,
                "events": events,
                "events_truncated": events_truncated,
                "approval_required": pending_approval,
            }
        result = {
            "conversation": self.public_conversation(conversation),
            "turn": final,
            "events": events,
            "events_truncated": events_truncated,
            "approval_required": pending_approval,
        }
        if not self.output.json_mode and pending_approval is None:
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
                if self._dispatch_project_input(current, message):
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
        if name == "permissions":
            return self._permissions(conversation, command), False
        if name == "queue":
            self._queue_command(conversation, command)
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
            return self._inherit_cli_context(selected, conversation), False
        if name == "continue":
            self._arity(command, 0, "/continue")
            rows = self.client.list_conversations(
                project_id=conversation.get("project_id"),
                agent_id=conversation.get("agent_id"),
                status="active",
                mode=conversation.get("_cli_mode"),
                limit=1,
            )
            if not rows:
                raise CliError("CONVERSATION_NOT_FOUND", "no active conversation found")
            self.renderer.header(self._metadata(rows[0]))
            return self._inherit_cli_context(rows[0], conversation), False
        if name == "new":
            self._arity(command, 0, "/new")
            if conversation.get("_cli_project_session_id"):
                raise CliError(
                    "PROJECT_SESSION_STABLE",
                    "Project Sessions rotate conversations through 'nico project open/session'",
                    exit_code=2,
                )
            selected = self.client.create_conversation(
                project_id=(
                    None
                    if conversation.get("_cli_mode") == "personal"
                    else conversation["project_id"]
                ),
                agent_id=conversation["agent_id"],
                mode=conversation.get("_cli_mode", "project"),
                agent_version_id=conversation["agent_version_id"],
                title="New conversation",
                idempotency_key=str(uuid4()),
            )
            selected = self._with_mode(selected, conversation.get("_cli_mode", "project"))
            self.renderer.header(self._metadata(selected))
            return self._inherit_cli_context(selected, conversation), False
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
            return self._inherit_cli_context(selected, conversation), False
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

        queue = self.client.get_conversation_queue(conversation["id"])
        turn = self._control_turn(conversation, queue)
        run_id = turn.get("run_id") if turn else None
        if name == "status":
            self.output.emit(
                {"conversation": conversation, "queue": queue, "turn": turn},
                title="Status",
            )
        elif name == "agent":
            self.output.emit(self.client.get_agent(conversation["agent_id"]), title="Agent")
        elif name == "version":
            versions = self.client.list_agent_versions(conversation["agent_id"])
            value = next(
                (item for item in versions if item.get("id") == conversation["agent_version_id"]),
                {"id": conversation["agent_version_id"]},
            )
            self.output.emit(value, title="Agent Version")
        elif name == "guide":
            if not command.raw_args:
                raise CliError("SLASH_ARGUMENT_REQUIRED", "usage: /guide TEXT", exit_code=2)
            project_id, session_id = self._project_context(conversation)
            self._require_active_turn(turn)
            value = self.client.create_run_intervention(
                project_id,
                session_id,
                str(turn["run_id"]),
                content=command.raw_args,
                expected_run_revision=int(turn["run_revision"]),
                idempotency_key=str(uuid4()),
            )
            self.output.emit(value, title="Run Guidance Pending")
        elif name == "escalate":
            if not command.raw_args:
                raise CliError("SLASH_ARGUMENT_REQUIRED", "usage: /escalate TEXT", exit_code=2)
            project_id, session_id = self._project_context(conversation)
            value = self.client.escalate_project_change(
                project_id,
                session_id,
                content=command.raw_args,
                max_steps=64,
                token_budget=None,
                timeout_seconds=None,
                idempotency_key=str(uuid4()),
            )
            self.output.emit(value, title="Project Change Escalated")
        elif name == "interventions":
            project_id, session_id = self._project_context(conversation)
            self._require_turn(turn)
            values = self.client.list_run_interventions(project_id, session_id, str(turn["run_id"]))
            self.output.table(
                values,
                title="Run Interventions",
                columns=["id", "kind", "status", "revision", "content", "created_at"],
            )
        elif name == "withdraw":
            if not command.args:
                raise CliError(
                    "INVALID_SLASH_ARGUMENTS",
                    "usage: /withdraw ID [REASON]",
                    exit_code=2,
                )
            project_id, session_id = self._project_context(conversation)
            self._require_turn(turn)
            values = self.client.list_run_interventions(project_id, session_id, str(turn["run_id"]))
            intervention = next(
                (item for item in values if str(item.get("id")) == command.args[0]),
                None,
            )
            if intervention is None:
                raise CliError(
                    "RUN_INTERVENTION_NOT_FOUND",
                    "the requested intervention is not attached to the current Run",
                    exit_code=2,
                )
            value = self.client.withdraw_run_intervention(
                project_id,
                session_id,
                str(turn["run_id"]),
                command.args[0],
                expected_intervention_revision=int(intervention["revision"]),
                reason=" ".join(command.args[1:]) or None,
            )
            self.output.emit(value, title="Run Guidance Withdrawn")
        elif name == "cancel":
            self._require_turn(turn)
            if turn["run_status"] in _TERMINAL:
                raise CliError("RUN_TERMINAL", "the current Turn is already terminal")
            value = self.client.cancel_conversation_turn(
                turn["id"], expected_run_revision=turn["run_revision"]
            )
            self.output.emit(value, title="Cancelled")
        elif name == "retry":
            turn = queue.get("pause_turn")
            if turn is None:
                raise CliError(
                    "CONVERSATION_RETRY_NOT_PAUSED",
                    "the Conversation queue has no pause-causing Turn to retry",
                )
            accepted = self.client.retry_conversation_turn(
                turn["id"],
                expected_run_id=turn["run_id"],
                expected_run_revision=turn["run_revision"],
            )
            pending_approval: dict[str, Any] | None = None
            with self.renderer.progress() as progress:
                for event in self.client.stream_run_events(
                    accepted["run_id"], on_connection=progress.connection
                ):
                    progress.event(event)
                    should_stop, approval = self._handle_stream_approval(event, progress)
                    if should_stop:
                        pending_approval = approval
                        break
                if pending_approval is None:
                    final = self.client.get_conversation_turn(accepted["id"])
            if pending_approval is None:
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

    def _dispatch_project_input(self, conversation: dict[str, Any], message: str) -> bool:
        del conversation, message
        return False

    def _permissions(
        self,
        conversation: dict[str, Any],
        command: SlashCommand,
    ) -> dict[str, Any]:
        if len(command.args) > 1:
            raise CliError(
                "INVALID_SLASH_ARGUMENTS",
                "usage: /permissions [ask|auto-medium|auto-all]",
                exit_code=2,
            )
        current = self.client.get_conversation(conversation["id"])
        current_mode = str(current.get("approval_mode") or "ask")
        if command.args:
            target = command.args[0]
        else:
            self.output.table(
                [
                    {
                        "number": index,
                        "mode": mode,
                        "behavior": {
                            "ask": "confirm medium and high Tool calls",
                            "auto-medium": "auto-approve medium; confirm high",
                            "auto-all": "auto-approve authorized medium and high",
                        }[mode],
                        "current": mode == current_mode,
                    }
                    for index, mode in enumerate(_APPROVAL_MODES, start=1)
                ],
                title="Conversation Permissions",
                columns=["number", "mode", "behavior", "current"],
            )
            answer = self.selection_prompt(
                f"Permission mode number or key [{current_mode}]: "
            ).strip()
            if not answer:
                target = current_mode
            elif answer.isdecimal() and 1 <= int(answer) <= len(_APPROVAL_MODES):
                target = _APPROVAL_MODES[int(answer) - 1]
            else:
                target = answer
        if target not in _APPROVAL_MODES:
            raise CliError(
                "INVALID_PERMISSION_MODE",
                "permission mode must be ask, auto-medium, or auto-all",
                exit_code=2,
            )
        if target == current_mode:
            self.output.emit(
                {"approval_mode": current_mode, "changed": False},
                title="Conversation Permissions",
            )
            return self._inherit_cli_context(current, conversation)

        rank = {mode: index for index, mode in enumerate(_APPROVAL_MODES)}
        if rank[target] > rank[current_mode]:
            confirmed = self.selection_prompt(
                f"Allow {target} for future Runs in this Conversation? [y/N]: "
            ).strip()
            if confirmed.lower() not in {"y", "yes"}:
                self.output.emit(
                    {"approval_mode": current_mode, "changed": False},
                    title="Conversation Permissions",
                )
                return self._inherit_cli_context(current, conversation)

        selected = self.client.update_conversation(
            conversation["id"],
            expected_revision=int(current["revision"]),
            approval_mode=target,
        )
        self.output.emit(
            {
                "approval_mode": selected["approval_mode"],
                "changed": True,
                "applies_to": "Runs that have not created a RuntimeSession",
            },
            title="Conversation Permissions",
        )
        return self._inherit_cli_context(selected, conversation)

    def _queue_command(self, conversation: dict[str, Any], command: SlashCommand) -> None:
        if len(command.args) > 2:
            raise CliError(
                "INVALID_SLASH_ARGUMENTS",
                "usage: /queue [resume|cancel TURN]",
                exit_code=2,
            )
        queue = self.client.get_conversation_queue(conversation["id"])
        if not command.args:
            self._render_queue(queue)
            return
        action = command.args[0]
        if action == "resume" and len(command.args) == 1:
            queue = self.client.resume_conversation_queue(
                conversation["id"],
                expected_revision=int(queue["revision"]),
                idempotency_key=str(uuid4()),
            )
            self._render_queue(queue)
            return
        if action == "cancel" and len(command.args) == 2:
            turn = self._queue_turn(queue, command.args[1])
            value = self.client.cancel_conversation_turn(
                str(turn["id"]),
                expected_run_revision=int(turn["run_revision"]),
            )
            self.output.emit(value, title="Queued Turn cancelled")
            return
        raise CliError(
            "INVALID_SLASH_ARGUMENTS",
            "usage: /queue [resume|cancel TURN]",
            exit_code=2,
        )

    def _render_queue(self, queue: dict[str, Any]) -> None:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for position, turn in (
            ("active", queue.get("active_turn")),
            ("paused", queue.get("pause_turn")),
        ):
            if isinstance(turn, dict) and str(turn.get("id")) not in seen:
                seen.add(str(turn.get("id")))
                rows.append({"position": position, **turn})
        for turn in queue.get("queued_turns") or []:
            if isinstance(turn, dict) and str(turn.get("id")) not in seen:
                seen.add(str(turn.get("id")))
                rows.append({"position": "queued", **turn})
        if not rows:
            rows.append(
                {
                    "position": "empty",
                    "sequence": None,
                    "status": None,
                    "user_input": None,
                    "id": None,
                }
            )
        title = (
            f"Conversation Queue · {queue.get('state', 'unknown')} · "
            f"{queue.get('queued_count', 0)}/{queue.get('capacity', 20)} queued"
        )
        if queue.get("pause_reason"):
            title += f" · {queue['pause_reason']}"
        self.output.table(
            rows,
            title=title,
            columns=["position", "sequence", "status", "user_input", "id"],
        )

    @staticmethod
    def _queue_turn(queue: dict[str, Any], reference: str) -> dict[str, Any]:
        values = [
            queue.get("active_turn"),
            queue.get("head_turn"),
            queue.get("pause_turn"),
            *(queue.get("queued_turns") or []),
        ]
        turns = {
            str(turn["id"]): turn
            for turn in values
            if isinstance(turn, dict) and turn.get("id") is not None
        }
        matches = [
            turn
            for turn_id, turn in turns.items()
            if turn_id == reference
            or turn_id.startswith(reference)
            or str(turn.get("sequence")) == reference
        ]
        if len(matches) != 1:
            raise CliError(
                "CONVERSATION_QUEUE_TURN_NOT_FOUND",
                "queue Turn must match one sequence or unique Turn ID prefix",
                exit_code=2,
            )
        return matches[0]

    @staticmethod
    def _project_context(conversation: dict[str, Any]) -> tuple[str, str]:
        project_id = conversation.get("_cli_project_id")
        session_id = conversation.get("_cli_project_session_id")
        if not project_id or not session_id:
            raise CliError(
                "PROJECT_SESSION_REQUIRED",
                "this command is only available inside 'nico project open/session'",
                exit_code=2,
            )
        return str(project_id), str(session_id)

    @staticmethod
    def _require_active_turn(turn: dict[str, Any] | None) -> None:
        ChatRunner._require_turn(turn)
        if turn["run_status"] in _TERMINAL:
            raise CliError("RUN_TERMINAL", "the current Turn is already terminal")

    @staticmethod
    def _inherit_cli_context(selected: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        return {
            **selected,
            **{key: value for key, value in current.items() if key.startswith("_cli_")},
        }

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
        project = (
            None
            if conversation.get("_cli_mode") == "personal"
            else self.client.get_project(conversation["project_id"])
        )
        agent = self.client.get_agent(conversation["agent_id"])
        versions = self.client.list_agent_versions(conversation["agent_id"])
        version = next(
            (item for item in versions if item.get("id") == conversation["agent_version_id"]),
            {},
        )
        policy = version.get("tool_policy") or {}
        return {
            "project": (project.get("name") or project.get("id")) if project else None,
            "agent": agent.get("display_name") or agent.get("name") or agent.get("id"),
            "version": version.get("version") or conversation.get("agent_version_id"),
            "runtime": (
                version.get("runtime_provider") or version.get("execution_mode") or "default"
            ),
            "tools": policy.get("allow") or policy.get("tools") or [],
        }

    def _control_turn(
        self,
        conversation: dict[str, Any],
        queue: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        snapshot = queue or self.client.get_conversation_queue(conversation["id"])
        current = snapshot.get("active_turn") or snapshot.get("head_turn")
        if isinstance(current, dict):
            return current
        rows = self.client.list_conversation_turns(conversation["id"], limit=500)
        return rows[-1] if rows else None

    def _can_prompt_for_approval(self) -> bool:
        return not self.output.json_mode and sys.stdin.isatty()

    def _decide_interactively(self, approval: dict[str, Any]) -> bool:
        if approval.get("status") != "requested":
            self.output.emit(approval, title="Tool approval already decided")
            return True
        return ApprovalCoordinator(
            self.client,
            self.output,
            interactive=self._can_prompt_for_approval(),
            prompt=self.approval_prompt,
        ).decide(approval)

    def _handle_stream_approval(
        self,
        event: dict[str, Any],
        progress: Any,
    ) -> tuple[bool, dict[str, Any] | None]:
        if event.get("type") != "ApprovalRequested":
            return False, None
        payload = event.get("payload") or {}
        approval_id = payload.get("approval_id")
        if not approval_id:
            return False, None
        approval = self.client.get_tool_approval(str(approval_id))
        if not self.output.json_mode:
            progress.pause()
            self.renderer.approval(approval)
        if self._can_prompt_for_approval() and self._decide_interactively(approval):
            progress.resume()
            return False, None
        return True, approval

    def _resume_pending_approval(self, conversation: dict[str, Any]) -> None:
        turn = self._control_turn(conversation)
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
            with self.renderer.progress(initial="Continuing") as progress:
                for event in self.client.stream_run_events(
                    str(turn["run_id"]),
                    after_sequence=after_sequence,
                    on_connection=progress.connection,
                ):
                    progress.event(event)
                    should_stop, _approval = self._handle_stream_approval(event, progress)
                    if should_stop:
                        return
                final = self.client.get_conversation_turn(turn["id"])
            self.renderer.final(final)

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
