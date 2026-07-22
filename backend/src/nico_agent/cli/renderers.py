"""Rich, scrolling renderers for Nico execution and inspection data."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit

from rich.columns import Columns
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from nico_agent.cli.logo import capabilities, terminal_cat
from nico_agent.cli.output import Output

_PROTOCOL_LABELS = {
    "openai_compatible": "OpenAI-compatible",
    "anthropic_messages": "Anthropic Messages",
    "google_gemini": "Google Gemini",
}

_VISIBLE_TOOL_EVENTS = {
    "ToolCallStarted",
    "ToolCallSucceeded",
    "ToolCallFailed",
    "ToolCallTimedOut",
    "ToolCallCancelled",
    "ToolCallRejected",
}
_VISIBLE_ARTIFACT_EVENTS = {"ArtifactAvailable"}
_VISIBLE_RUN_EVENTS = {"RunFailed", "RunCancelled", "RunTimedOut"}
_VISIBLE_EXECUTION_EVENTS = _VISIBLE_TOOL_EVENTS | _VISIBLE_ARTIFACT_EVENTS | _VISIBLE_RUN_EVENTS
_TOOL_TERMINAL_EVENTS = _VISIBLE_TOOL_EVENTS - {"ToolCallStarted"}
_FAILURE_EVENT_LABELS = {
    "ToolCallFailed": "Tool failed",
    "ToolCallTimedOut": "Tool timed out",
    "ToolCallCancelled": "Tool cancelled",
    "ToolCallRejected": "Tool rejected",
    "RunFailed": "Run failed",
    "RunCancelled": "Run cancelled",
    "RunTimedOut": "Run timed out",
}

_PREPARING_EVENTS = {
    "TaskCreated",
    "RunCreated",
    "ConversationTurnQueued",
    "RuntimeProviderResolved",
    "RuntimeSessionBound",
    "RuntimeSessionCreated",
    "RuntimeRunStarted",
    "RunStarted",
}
_PLANNING_EVENTS = {
    "RunPlanningStarted",
    "RuntimePlanCreated",
    "RuntimePlanStatusChanged",
    "RuntimePlanStepStarted",
    "RuntimePlanStepCompleted",
    "RuntimePlanStepFailed",
}
_THINKING_EVENTS = {
    "RuntimeModelCallStarted",
    "RuntimeModelCallCompleted",
    "RuntimeModelCallFailed",
    "RuntimeModelOutputDelta",
}
_REFLECTING_EVENTS = {"RuntimeReflectionCompleted", "RuntimeEvaluationCompleted"}
_FINALIZING_EVENTS = {"RuntimeOutputDelta", "RuntimeRunCompleted", "RunCompleted"}


class ProviderSetupRenderer:
    """Render the guided Provider setup without owning its decisions."""

    def __init__(self, output: Output) -> None:
        self.output = output

    def welcome(self) -> None:
        if self.output.json_mode:
            return
        body = Text()
        body.append("Connect a model Provider\n", style="bold")
        body.append(
            "Choose a preset, or connect any compatible cloud or local endpoint.",
            style="dim",
        )
        self.output.out.print(
            Panel(body, title="Nico Setup", border_style="#7895ac", padding=(1, 2))
        )

    @contextmanager
    def progress(self, message: str) -> Iterator[None]:
        if self.output.json_mode:
            yield
            return
        label = Text(message, style="bold #7895ac")
        with self.output.out.status(label, spinner="dots"):
            yield

    def providers(self, providers: Iterable[Mapping[str, Any]]) -> None:
        if self.output.json_mode:
            return
        materialized = list(providers)
        table = Table(
            title="Model Providers",
            header_style="bold #7895ac",
            border_style="#7895ac",
            row_styles=("", "dim"),
            expand=True,
        )
        table.add_column("#", justify="right", style="bold #d0a84e", width=3)
        table.add_column("Provider", style="bold", min_width=18)
        table.add_column("Protocol", min_width=18)
        table.add_column("Popular models", overflow="fold")
        for index, provider in enumerate(materialized, start=1):
            models = [str(value) for value in provider.get("recommended_models") or []]
            table.add_row(
                str(index),
                str(provider.get("display_name") or provider.get("key")),
                _PROTOCOL_LABELS.get(
                    str(provider.get("protocol")), str(provider.get("protocol") or "—")
                ),
                ", ".join(models[:3]) or "Discover after connecting",
            )
        table.add_section()
        table.add_row(
            str(len(materialized) + 1),
            "Other Provider",
            "OpenAI / local",
            "Custom URL and exact model ID",
            style="#d0a84e",
        )
        self.output.out.print(table)
        self.output.out.print(
            "[dim]Tip: API keys use a hidden prompt and are never sent as CLI metadata.[/dim]"
        )

    def protocols(self) -> None:
        if self.output.json_mode:
            return
        table = Table(title="API Protocol", header_style="bold #7895ac", border_style="#7895ac")
        table.add_column("#", justify="right", style="bold #d0a84e")
        table.add_column("Protocol")
        table.add_column("Use for")
        table.add_row("1", "OpenAI-compatible", "Ollama, vLLM, LM Studio, most gateways")
        table.add_row("2", "Anthropic Messages", "Claude-compatible endpoints")
        table.add_row("3", "Google Gemini", "Gemini-compatible endpoints")
        self.output.out.print(table)

    def models(self, models: Iterable[str], recommendations: set[str]) -> None:
        if self.output.json_mode:
            return
        table = Table(
            title="Available Models",
            header_style="bold #7895ac",
            border_style="#7895ac",
            expand=True,
        )
        table.add_column("#", justify="right", style="bold #d0a84e", width=3)
        table.add_column("Model ID", style="bold")
        table.add_column("Status", width=14)
        for index, model in enumerate(models, start=1):
            table.add_row(
                str(index),
                model,
                "★ Recommended" if model in recommendations else "Discovered",
            )
        self.output.out.print(table)


class ProjectRenderer:
    """Render auditable Project work without implying access to private reasoning."""

    def __init__(self, output: Output) -> None:
        self.output = output

    def timeline(self, entries: Iterable[Mapping[str, Any]]) -> None:
        if self.output.json_mode:
            return
        table = Table(
            title="Project Work Timeline",
            header_style="bold #7895ac",
            border_style="#7895ac",
            expand=True,
        )
        table.add_column("#", justify="right", style="dim", width=6)
        table.add_column("Type", style="bold", width=12)
        table.add_column("Event", min_width=20)
        table.add_column("Auditable facts", overflow="fold")
        table.add_column("References", overflow="fold")
        for entry in entries:
            kind = str(entry.get("kind") or "state")
            style, symbol = _project_kind_style(kind)
            table.add_row(
                str(entry.get("sequence") or "—"),
                Text(f"{symbol} {kind}", style=style),
                str(entry.get("event_type") or "—"),
                _compact_mapping(entry.get("facts")),
                _compact_mapping(entry.get("links")),
            )
        self.output.out.print(table)
        self.output.out.print(
            "[dim]Timeline shows persisted plans, actions, results, and references; "
            "private model reasoning is never exposed.[/dim]"
        )

    def cycles(self, cycles: Iterable[Mapping[str, Any]]) -> None:
        if self.output.json_mode:
            return
        materialized = list(cycles)
        summary = Table(
            title="Project Supervision",
            header_style="bold #7895ac",
            border_style="#7895ac",
            expand=True,
        )
        summary.add_column("Status", style="bold", width=12)
        summary.add_column("Trigger", width=10)
        summary.add_column("Scheduled")
        summary.add_column("Database facts", overflow="fold")
        summary.add_column("Lead narrative", overflow="fold")
        for cycle in materialized:
            summary.add_row(
                str(cycle.get("status") or "—"),
                str(cycle.get("trigger") or "—"),
                str(cycle.get("scheduled_for") or "—"),
                _compact_mapping(cycle.get("metrics")),
                str(cycle.get("narrative_summary") or "No model narrative"),
            )
        self.output.out.print(summary)


class ExecutionRenderer:
    """Render execution state without owning any API or domain behaviour."""

    def __init__(self, output: Output) -> None:
        self.output = output

    def progress(
        self,
        *,
        initial: str = "Queued",
        clock: Callable[[], float] = time.monotonic,
    ) -> ExecutionProgress:
        return ExecutionProgress(self, initial=initial, clock=clock)

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
        if metadata.get("project") is not None:
            details.add_row("Project", _label(metadata.get("project")))
        details.add_row("Tools", _label(metadata.get("tools"), default="policy default"))
        body = Columns([terminal_cat(caps), details], padding=(0, 3), expand=False)
        self.output.out.print(
            Panel(body, title="Nico Agent", border_style="#7895ac", padding=(0, 1))
        )

    def event(
        self,
        event: Mapping[str, Any],
        *,
        duration_seconds: float | None = None,
    ) -> None:
        if self.output.json_mode:
            return
        event_type = str(event.get("type") or event.get("event_type") or "Event")
        payload = _event_payload(event)
        if event_type.endswith("OutputDelta"):
            return
        if event_type not in _VISIBLE_EXECUTION_EVENTS:
            return
        if event_type == "ToolCallStarted":
            return
        line = Text()
        if event_type == "ToolCallSucceeded":
            detail = _safe_tool_detail(payload)
            line.append("✓ ", style="green")
            line.append(detail or "Tool completed")
            if duration := _duration_text(duration_seconds):
                line.append(f" ({duration})", style="dim")
        elif event_type in _VISIBLE_ARTIFACT_EVENTS:
            detail = _safe_value(payload.get("name"))
            line.append("✓ ", style="green")
            line.append(f"Saved {detail or 'artifact'}")
        else:
            label = _FAILURE_EVENT_LABELS[event_type]
            style = "yellow" if "Cancelled" in event_type else "red"
            line.append(f"{'■' if style == 'yellow' else '×'} ", style=style)
            line.append(label, style=f"bold {style}")
            if event_type in _TOOL_TERMINAL_EVENTS:
                detail = _safe_tool_detail(payload)
                if detail:
                    line.append(f": {detail}")
                if code := _safe_value(payload.get("code")):
                    line.append(f" ({code})")
                if duration := _duration_text(duration_seconds):
                    line.append(f" ({duration})", style="dim")
            elif detail := _safe_value(payload.get("code")):
                line.append(f": {detail}")
        self.output.out.print(line)

    def final(self, turn: Mapping[str, Any]) -> None:
        if self.output.json_mode:
            return
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
            json.dumps(_approval_preview(approval), ensure_ascii=False, indent=2),
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


class ExecutionProgress:
    """Project raw execution events into one scoped human progress surface."""

    def __init__(
        self,
        renderer: ExecutionRenderer,
        *,
        initial: str,
        clock: Callable[[], float],
    ) -> None:
        self.renderer = renderer
        self.output = renderer.output
        self._clock = clock
        self._started_at = clock()
        self._activity_label = initial
        self._activity_detail: str | None = None
        self._connection: str | None = None
        self._tool_started_at: dict[str, float] = {}
        self._reported_reconnects: set[tuple[int, int]] = set()
        self._live: Live | None = None
        self._running = False
        self._stopped = False
        self._printed_initial = False
        self._tty = bool(self.output.out.is_terminal)

    @property
    def activity(self) -> str:
        if self._activity_detail:
            return f"{self._activity_label} {self._activity_detail}"
        return self._activity_label

    @property
    def running(self) -> bool:
        return self._running

    def __enter__(self) -> ExecutionProgress:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def start(self) -> None:
        if self._running or self._stopped:
            return
        self._running = True
        if self.output.json_mode:
            return
        if not self._tty:
            if not self._printed_initial:
                self.output.out.print(Text(f"› {self.activity}", style="bold #7895ac"))
                self._printed_initial = True
            return
        self._live = Live(
            console=self.output.out,
            get_renderable=self._renderable,
            refresh_per_second=8,
            transient=True,
            redirect_stdout=False,
            redirect_stderr=False,
        )
        self._live.start(refresh=True)

    def pause(self) -> None:
        if not self._running:
            return
        live, self._live = self._live, None
        if live is not None:
            live.stop()
        self._running = False

    def resume(self) -> None:
        if self._stopped or self._running:
            return
        self._set_activity("Continuing")
        self.start()

    def stop(self) -> None:
        if self._stopped:
            return
        self.pause()
        self._stopped = True

    def event(self, event: Mapping[str, Any]) -> None:
        event_type = str(event.get("type") or event.get("event_type") or "")
        normalized_type = event_type.replace("_", "")
        payload = _event_payload(event)

        if normalized_type == "ToolCallStarted":
            detail = _safe_tool_detail(payload)
            if key := _tool_event_key(payload):
                self._tool_started_at[key] = self._clock()
            self._set_activity("Running", detail or "tool")
            return
        if normalized_type in _TOOL_TERMINAL_EVENTS:
            duration = None
            if key := _tool_event_key(payload):
                started_at = self._tool_started_at.pop(key, None)
                if started_at is not None:
                    duration = max(0.0, self._clock() - started_at)
            self.renderer.event(event, duration_seconds=duration)
            self._set_activity("Continuing")
            return
        if normalized_type == "RuntimeToolCallStarted":
            self._set_activity("Running", _safe_tool_detail(payload) or "tool")
            return
        if normalized_type == "RuntimeToolCallCompleted":
            self._set_activity("Continuing")
            return
        if normalized_type in _VISIBLE_ARTIFACT_EVENTS | _VISIBLE_RUN_EVENTS:
            self.renderer.event(event)
        if normalized_type in _PREPARING_EVENTS:
            self._set_activity("Preparing")
        elif normalized_type in _PLANNING_EVENTS:
            self._set_activity("Planning")
        elif normalized_type in _THINKING_EVENTS:
            self._set_activity("Thinking")
        elif normalized_type == "RuntimeStepStarted":
            self._set_activity("Working")
        elif normalized_type == "RuntimeDelegationStarted":
            self._set_activity("Delegating")
        elif normalized_type == "RuntimeDelegationAccepted":
            self._set_activity("Waiting for subagent")
        elif normalized_type == "RuntimeDelegationResultReceived":
            self._set_activity("Continuing")
        elif normalized_type in _REFLECTING_EVENTS:
            self._set_activity("Reflecting")
        elif normalized_type in _FINALIZING_EVENTS:
            self._set_activity("Finalizing")

    def connection(
        self,
        state: str,
        attempt: int | None = None,
        max_attempts: int | None = None,
    ) -> None:
        if state == "reconnecting":
            current = max(1, attempt or 1)
            total = max(current, max_attempts or current)
            self._connection = f"reconnecting {current}/{total}"
            if not self.output.json_mode and not self._tty:
                key = (current, total)
                if key not in self._reported_reconnects:
                    self.output.out.print(f"› Reconnecting {current}/{total}")
                    self._reported_reconnects.add(key)
            return
        self._connection = None

    def status_text(self, width: int) -> str:
        available = max(1, width)
        phase = self._activity_label
        detail = self._activity_detail
        elapsed = _elapsed_text(max(0.0, self._clock() - self._started_at))
        connection = self._connection

        activity = f"{phase} {detail}" if detail else phase
        candidate = _compose_status(activity, elapsed=elapsed, connection=connection)
        if len(candidate) <= available:
            return candidate
        candidate = _compose_status(phase, elapsed=elapsed, connection=connection)
        if len(candidate) <= available:
            return candidate
        candidate = _compose_status(phase, elapsed=None, connection=connection)
        if len(candidate) <= available:
            return candidate
        if connection:
            compact = f"{phase} | retry {connection.rsplit(' ', 1)[-1]}"
            if len(compact) <= available:
                return compact
        return _ellipsize(candidate, available)

    def _set_activity(self, label: str, detail: str | None = None) -> None:
        self._activity_label = label
        self._activity_detail = _safe_value(detail) if detail else None

    def _renderable(self) -> Spinner:
        width = max(1, self.output.out.width - 2)
        status = Text(self.status_text(width), style="bold #7895ac", no_wrap=True)
        return Spinner("dots", status, style="#7895ac")


def chat_footer_status(
    *,
    model: str,
    current_approval_mode: str | None,
    next_approval_mode: str,
    activity: str,
    queued_count: int,
    queue_state: str,
    pause_reason: str | None,
    width: int,
) -> str:
    """Compose a width-bounded footer from already-curated session facts."""

    available = max(1, width)
    safe_model = _safe_value(model, limit=200) or "default"
    safe_activity = _safe_value(activity, limit=100) or "Idle"
    permission = (
        f"{current_approval_mode} → {next_approval_mode}"
        if current_approval_mode and current_approval_mode != next_approval_mode
        else (current_approval_mode or next_approval_mode)
    )
    queue = (
        f"paused ({pause_reason or 'attention required'})"
        if queue_state == "paused"
        else f"queued {max(0, queued_count)}"
    )
    verbose = f"model {safe_model} · permission {permission} · {safe_activity} · {queue}"
    if len(verbose) <= available:
        return verbose

    modes = {"ask": "a", "auto-medium": "am", "auto-all": "aa"}
    current_short = modes.get(current_approval_mode or "", "-")
    next_short = modes.get(next_approval_mode, "-")
    permission_short = (
        f"{current_short}→{next_short}"
        if current_approval_mode and current_approval_mode != next_approval_mode
        else modes.get(current_approval_mode or next_approval_mode, "-")
    )
    phase = safe_activity.split(" ", 1)[0]
    queue_short = "q:paused" if queue_state == "paused" else f"q:{max(0, queued_count)}"
    suffix = f" p:{permission_short} {phase} {queue_short}"
    model_width = max(1, available - len(suffix) - 2)
    compact = f"m:{_ellipsize(safe_model, model_width)}{suffix}"
    return _ellipsize(compact, available)


def _safe_tool_detail(payload: Mapping[str, Any]) -> str:
    value = _safe_value(payload.get("tool") or payload.get("tool_name") or payload.get("name"))
    reference = value.lower()
    if reference.startswith("web.search@") or reference == "web.search":
        return "Web search"
    if reference.startswith("web.fetch@") or reference == "web.fetch":
        return "Reading source"
    return value


def _approval_preview(approval: Mapping[str, Any]) -> Mapping[str, Any]:
    arguments = approval.get("arguments")
    safe_arguments = arguments if isinstance(arguments, Mapping) else {}
    tool_name = str(approval.get("tool_name") or "").lower()
    if tool_name == "web.search" or tool_name.startswith("web.search@"):
        return {"query": _safe_value(safe_arguments.get("query"), limit=300)}
    if tool_name == "web.fetch" or tool_name.startswith("web.fetch@"):
        raw_url = safe_arguments.get("url")
        if not isinstance(raw_url, str):
            return {"origin": "invalid target"}
        parsed = urlsplit(raw_url)
        origin = (
            f"{parsed.scheme}://{parsed.netloc}"
            if parsed.scheme and parsed.netloc
            else "invalid target"
        )
        return {"origin": _safe_value(origin, limit=300)}
    return safe_arguments


def _tool_event_key(payload: Mapping[str, Any]) -> str | None:
    for key in ("run_step_id", "tool_call_id"):
        if value := _safe_value(payload.get(key)):
            return f"{key}:{value}"
    return None


def _safe_value(value: Any, *, limit: int = 120) -> str:
    if not isinstance(value, str) or not value:
        return ""
    compact = " ".join(value.split())
    return _ellipsize(compact, limit)


def _duration_text(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.1f}s"


def _elapsed_text(value: float) -> str:
    total = int(value)
    minutes, seconds = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}:{seconds:02d}"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def _compose_status(activity: str, *, elapsed: str | None, connection: str | None) -> str:
    value = activity
    if elapsed:
        value += f" · {elapsed}"
    if connection:
        value += f" | {connection}"
    return value


def _ellipsize(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    if width <= 1:
        return "…"[:width]
    return f"{value[: width - 1]}…"


def _event_payload(event: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        return {}
    projected = payload.get("payload")
    if not isinstance(projected, Mapping):
        return payload
    return {**payload, **projected}


def _label(value: Any, *, default: str = "—") -> str:
    if value in (None, "", []):
        return default
    if isinstance(value, (list, tuple)):
        return ", ".join(map(str, value))
    return str(value)


def _project_kind_style(kind: str) -> tuple[str, str]:
    return {
        "plan": ("#7895ac", "▸"),
        "tool": ("#d0a84e", "◆"),
        "artifact": ("green", "◈"),
        "delegation": ("magenta", "↳"),
        "task": ("cyan", "□"),
        "run": ("blue", "●"),
        "conversation": ("white", "›"),
        "state": ("dim", "·"),
    }.get(kind, ("dim", "·"))


def _compact_mapping(value: Any) -> str:
    if not isinstance(value, Mapping) or not value:
        return "—"
    return ", ".join(f"{key}={item}" for key, item in value.items())
