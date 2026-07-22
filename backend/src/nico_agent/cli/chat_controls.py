"""Conversation-scoped permission and queue controls for interactive chat."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

from nico_agent.cli.client import NicoApiClient
from nico_agent.cli.errors import CliError
from nico_agent.cli.output import Output
from nico_agent.cli.slash import SlashCommand

_APPROVAL_MODES = ("ask", "auto-medium", "auto-all")


class ChatControls:
    """Render and mutate durable Conversation controls."""

    def __init__(
        self,
        client: NicoApiClient,
        output: Output,
        *,
        selection_prompt: Callable[[str], str],
    ) -> None:
        self.client = client
        self.output = output
        self.selection_prompt = selection_prompt

    def permissions(
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

    def queue(self, conversation: dict[str, Any], command: SlashCommand) -> None:
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
    def _inherit_cli_context(selected: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        return {
            **selected,
            **{key: value for key, value in current.items() if key.startswith("_cli_")},
        }
