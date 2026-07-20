"""Rich, scrolling renderers for Nico execution and inspection data."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from rich.columns import Columns
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from nico_agent.cli.logo import capabilities, coin_cat
from nico_agent.cli.output import Output


class ExecutionRenderer:
    """Render execution state without owning any API or domain behaviour."""

    def __init__(self, output: Output) -> None:
        self.output = output
        self._stream_open = False

    def header(self, metadata: Mapping[str, Any]) -> None:
        if self.output.json_mode:
            return
        caps = capabilities(
            self.output.stdout,
            width=self.output.out.width,
            no_color=self.output.no_color,
        )
        details = Table.grid(padding=(0, 1))
        details.add_column(style="bold #7895ac", no_wrap=True)
        details.add_column()
        details.add_row("Agent", _label(metadata.get("agent")))
        details.add_row("Version", _label(metadata.get("version")))
        details.add_row("Runtime", _label(metadata.get("runtime")))
        details.add_row("Project", _label(metadata.get("project")))
        details.add_row("Tools", _label(metadata.get("tools"), default="policy default"))
        body = Columns([coin_cat(caps), details], padding=(0, 3), expand=False)
        self.output.out.print(
            Panel(body, title="Nico Agent", border_style="#7895ac", padding=(0, 1))
        )

    def event(self, event: Mapping[str, Any]) -> None:
        if self.output.json_mode:
            return
        event_type = str(event.get("type") or event.get("event_type") or "Event")
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        sequence = event.get("sequence", "?")
        if event_type.endswith("OutputDelta"):
            delta = payload.get("message") or payload.get("delta") or payload.get("content")
            if delta:
                self.output.out.print(str(delta), end="")
                self._stream_open = True
            return
        self._finish_stream_line()
        style, symbol = _event_style(event_type)
        detail = _event_detail(event_type, payload)
        line = Text()
        line.append(f"{symbol} ", style=style)
        line.append(event_type, style=f"bold {style}")
        line.append(f"  #{sequence}", style="dim")
        if detail:
            line.append(f"  {detail}")
        self.output.out.print(line)

    def final(self, turn: Mapping[str, Any]) -> None:
        if self.output.json_mode:
            return
        self._finish_stream_line()
        value = turn.get("assistant_output")
        if value is not None:
            if isinstance(value, Mapping):
                answer = value.get("answer") or value.get("message") or value.get("content")
                if isinstance(answer, str):
                    self.output.out.print(
                        Panel(Markdown(answer), title="Nico", border_style="#d0a84e")
                    )
                else:
                    self.output.emit(value, title="Nico")
            else:
                self.output.emit(value, title="Nico")
            return
        if turn.get("error") is not None:
            self.output.emit(turn["error"], title=f"Nico · {turn.get('status', 'failed')}")
            return
        self.output.emit(dict(turn), title="Nico Turn")

    def plans(self, plans: Iterable[Mapping[str, Any]]) -> None:
        self.output.table(
            plans,
            title="Plan Revisions",
            columns=["revision", "status", "objective", "reason", "created_at"],
        )

    def plan_steps(self, steps: Iterable[Mapping[str, Any]]) -> None:
        self.output.table(
            steps,
            title="Plan Steps",
            columns=["position", "step_key", "status", "title", "description"],
        )

    def run_steps(self, steps: Iterable[Mapping[str, Any]]) -> None:
        self.output.table(
            steps,
            title="Run Steps",
            columns=["sequence", "kind", "status", "started_at", "ended_at"],
        )

    def tools(self, calls: Iterable[Mapping[str, Any]]) -> None:
        self.output.table(
            calls,
            title="Tool Calls",
            columns=["tool_name", "tool_version", "status", "usage", "started_at"],
        )

    def approval(self, approval: Mapping[str, Any]) -> None:
        if self.output.json_mode:
            return
        details = Table.grid(padding=(0, 1))
        details.add_column(style="bold #7895ac", no_wrap=True)
        details.add_column()
        details.add_row("Tool", f"{approval.get('tool_name')}@{approval.get('tool_version')}")
        details.add_row("Risk", str(approval.get("risk_level") or "unknown"))
        details.add_row("Requester", str(approval.get("requester") or "—"))
        details.add_row("Expires", str(approval.get("expires_at") or "—"))
        arguments = Syntax(
            json.dumps(approval.get("arguments") or {}, ensure_ascii=False, indent=2),
            "json",
            word_wrap=True,
            background_color="default",
        )
        body = Table.grid(padding=(1, 0))
        body.add_row(details)
        body.add_row(arguments)
        body.add_row(Text("[1] Allow once   [2] Allow for this Run   [3] Reject"))
        self.output.out.print(Panel(body, title="Sensitive tool approval", border_style="#d0a84e"))

    def approvals(self, approvals: Iterable[Mapping[str, Any]]) -> None:
        self.output.table(
            approvals,
            title="Tool Approvals",
            columns=[
                "id",
                "tool_name",
                "risk_level",
                "status",
                "allowed_scope",
                "requester",
                "expires_at",
            ],
        )

    def artifacts(self, artifacts: Iterable[Mapping[str, Any]]) -> None:
        self.output.table(
            artifacts,
            title="Artifacts",
            columns=["id", "name", "artifact_type", "content_type", "size_bytes", "status"],
        )

    def usage(self, runtime: Mapping[str, Any] | None, run: Mapping[str, Any]) -> None:
        usage = (runtime or {}).get("usage") or {}
        rows = [{"source": "runtime", **usage}, {"source": "run cost", **(run.get("cost") or {})}]
        rows = [row for row in rows if len(row) > 1]
        self.output.table(rows, title="Usage")

    def _finish_stream_line(self) -> None:
        if self._stream_open:
            self.output.out.print()
            self._stream_open = False


def _event_style(event_type: str) -> tuple[str, str]:
    lowered = event_type.lower()
    if any(value in lowered for value in ("failed", "error", "timedout", "timed_out")):
        return "red", "×"
    if "cancel" in lowered:
        return "yellow", "■"
    if any(value in lowered for value in ("completed", "succeeded", "available")):
        return "green", "✓"
    if any(value in lowered for value in ("tool", "artifact")):
        return "#d0a84e", "◆"
    if any(value in lowered for value in ("plan", "step")):
        return "#7895ac", "▸"
    return "dim", "·"


def _event_detail(event_type: str, payload: Mapping[str, Any]) -> str:
    candidates = (
        "message",
        "name",
        "tool_name",
        "step_key",
        "status",
        "reason",
        "provider_name",
    )
    for key in candidates:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    if event_type in {"RunFailed", "StepFailed", "ToolCallFailed"} and payload.get("error"):
        return str(payload["error"])
    return ""


def _label(value: Any, *, default: str = "—") -> str:
    if value in (None, "", []):
        return default
    if isinstance(value, (list, tuple)):
        return ", ".join(map(str, value))
    return str(value)
