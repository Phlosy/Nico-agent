"""Shared persisted Tool approval decision and Run continuation helpers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

from nico_agent.cli.client import NicoApiClient
from nico_agent.cli.output import Output


class ApprovalCoordinator:
    def __init__(
        self,
        client: NicoApiClient,
        output: Output,
        *,
        interactive: bool,
        prompt: Callable[[str], str],
    ) -> None:
        self.client = client
        self.output = output
        self.interactive = interactive
        self.prompt = prompt

    def decide(
        self,
        approval: dict[str, Any],
        *,
        automatically_approve: bool = False,
    ) -> bool:
        """Persist one decision; return False when operator input is unavailable."""

        if approval.get("status") != "requested":
            return True
        if automatically_approve:
            choice = "2"
        elif not self.interactive:
            return False
        else:
            while True:
                try:
                    choice = self.prompt("allow › ").strip()
                except (EOFError, KeyboardInterrupt):
                    self.output.out.print("[yellow]审批仍保存在服务端；可稍后恢复该 Run。[/yellow]")
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
        if not self.output.json_mode:
            self.output.emit(value, title="Tool approval saved")
        return approved
