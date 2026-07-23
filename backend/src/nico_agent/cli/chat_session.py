"""Asynchronous scrolling TTY controller for durable Conversation queues."""

from __future__ import annotations

import asyncio
import shutil
import threading
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from prompt_toolkit import PromptSession
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.utils import get_cwidth

from nico_agent.cli.client import NicoApiClient
from nico_agent.cli.errors import CliError
from nico_agent.cli.renderers import (
    ExecutionProgress,
    chat_footer_fragments,
    execution_event_has_durable_output,
    execution_event_output_delta,
)
from nico_agent.cli.slash import parse_slash

if TYPE_CHECKING:
    from nico_agent.cli.chat import ChatRunner

_TERMINAL = {"completed", "failed", "cancelled", "timed_out"}
_APPROVAL_INTERRUPT = "\0nico-approval-interrupt"
_APPROVAL_FETCH_ATTEMPTS = 3
_APPROVAL_FETCH_RETRY_SECONDS = 0.2
_USER_PROMPT = FormattedText([("class:nico.user-label", "you › ")])
_APPROVAL_PROMPT = FormattedText([("class:nico.approval", "approval › ")])
_QUEUE_PREVIEW_MAX_CELLS = 72
_QUEUE_LABEL = "  ↳ queued · "


class InteractiveChatSession:
    """Keep the prompt available while a separate connection observes the queue head."""

    def __init__(
        self,
        runner: ChatRunner,
        conversation: dict[str, Any],
        prompt_session: PromptSession[str],
        *,
        metadata: dict[str, Any],
        client_factory: Callable[[], NicoApiClient] | None = None,
    ) -> None:
        self.runner = runner
        self.client = runner.client
        self.conversation = conversation
        self.prompt_session = prompt_session
        self.metadata = metadata
        self.client_factory = client_factory or self.client.fork
        self.queue: dict[str, Any] = {
            "state": "active",
            "queued_count": 0,
            "capacity": 20,
        }
        self.current_approval_mode: str | None = None
        self.next_approval_mode = str(conversation.get("approval_mode") or "ask")
        self.model = str(metadata.get("model") or "default")
        self.show_response_metrics = bool(metadata.get("show_response_metrics"))
        self.progress: ExecutionProgress = runner.renderer.progress(initial="Idle")
        self._api_lock = asyncio.Lock()
        self._background: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=256)
        self._consumer_task: asyncio.Task[None] | None = None
        self._watch_tasks: set[asyncio.Task[None]] = set()
        self._watch_client: NicoApiClient | None = None
        self._watch_run_id: str | None = None
        self._stop = threading.Event()
        self._approvals: deque[dict[str, Any]] = deque()
        self._approval_ready = asyncio.Event()
        self._seen_approvals: set[str] = set()
        self._rendered_turns: set[str] = set()
        self._draft: Document | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        await self.initialize()
        self._consumer_task = asyncio.create_task(
            self._consume_background(), name="nico-chat-events"
        )
        try:
            with patch_stdout(raw=True):
                while True:
                    if self._approvals and not await self._resolve_next_approval():
                        break
                    default = self._draft or Document("")
                    self._draft = None
                    approval_interrupt = asyncio.create_task(self._interrupt_composer_on_approval())
                    try:
                        message = await self.prompt_session.prompt_async(
                            self.composer_prompt,
                            bottom_toolbar=self.footer,
                            default=default,
                            refresh_interval=1.0,
                        )
                    except KeyboardInterrupt:
                        await self.cancel_active_turn()
                        continue
                    except EOFError:
                        break
                    finally:
                        approval_interrupt.cancel()
                        await asyncio.gather(approval_interrupt, return_exceptions=True)
                    if message == _APPROVAL_INTERRUPT:
                        continue
                    try:
                        command = parse_slash(message)
                        if command is not None:
                            if command.name == "exit":
                                break
                            if command.name == "retry":
                                await self.retry_pause_turn()
                            else:
                                previous_id = str(self.conversation["id"])
                                selected, should_exit = await self._api_call(
                                    lambda command=command: self.runner._slash(
                                        self.conversation, command
                                    )
                                )
                                if str(selected["id"]) != previous_id:
                                    self._stop_current_watch()
                                self.conversation = selected
                                if "_cli_show_response_metrics" in selected:
                                    self.show_response_metrics = bool(
                                        selected["_cli_show_response_metrics"]
                                    )
                                if should_exit:
                                    break
                                await self._refresh_state()
                                await self._ensure_watch()
                            continue
                        await run_in_terminal(
                            lambda message=message: self.runner.renderer.user_message(message)
                        )
                        await self.submit_message(message)
                    except CliError as exc:
                        await run_in_terminal(lambda exc=exc: self.runner.output.error(exc))
        finally:
            await self.close()

    async def initialize(self) -> None:
        self._loop = self._loop or asyncio.get_running_loop()
        await self._refresh_state(include_approvals=True)
        await self._ensure_watch()

    async def submit_message(self, message: str) -> dict[str, Any]:
        if not message.strip():
            raise CliError("EMPTY_CHAT_MESSAGE", "chat message cannot be empty", exit_code=2)
        turn = await self._api_call(
            lambda: self.client.create_conversation_turn(
                self.conversation["id"],
                message,
                idempotency_key=str(uuid4()),
            )
        )
        await self._refresh_state()
        await self._ensure_watch()
        return turn

    async def retry_pause_turn(self) -> dict[str, Any]:
        await self._refresh_state()
        turn = self.queue.get("pause_turn")
        if not isinstance(turn, dict):
            raise CliError(
                "CONVERSATION_RETRY_NOT_PAUSED",
                "the Conversation queue has no pause-causing Turn to retry",
            )
        accepted = await self._api_call(
            lambda: self.client.retry_conversation_turn(
                str(turn["id"]),
                expected_run_id=str(turn["run_id"]),
                expected_run_revision=int(turn["run_revision"]),
            )
        )
        await run_in_terminal(lambda: self.runner.renderer.notice("Retry queued"))
        await self._refresh_state()
        await self._ensure_watch()
        return accepted

    async def cancel_active_turn(self) -> None:
        await self._refresh_state()
        turn = self.queue.get("active_turn")
        if not isinstance(turn, dict) and self.queue.get("state") == "active":
            turn = self.queue.get("head_turn")
        if not isinstance(turn, dict):
            return
        try:
            await self._api_call(
                lambda: self.client.cancel_conversation_turn(
                    str(turn["id"]),
                    expected_run_revision=int(turn["run_revision"]),
                )
            )
        except CliError as exc:
            if exc.code not in {
                "REVISION_CONFLICT",
                "INVALID_STATE_TRANSITION",
                "RUN_TERMINAL",
            }:
                raise
            return
        await run_in_terminal(
            lambda: self.runner.renderer.event({"type": "RunCancelled", "payload": {}})
        )
        await self._refresh_state()

    def footer(self) -> FormattedText:
        width = shutil.get_terminal_size((80, 24)).columns
        activity = self.progress.status_text(width) if self._watch_run_id is not None else "Idle"
        return FormattedText(
            [
                (f"class:bottom-toolbar.{style}", text)
                for style, text in chat_footer_fragments(
                    model=self.model,
                    current_approval_mode=self.current_approval_mode,
                    next_approval_mode=self.next_approval_mode,
                    activity=activity,
                    queued_count=int(self.queue.get("queued_count") or 0),
                    queue_state=str(self.queue.get("state") or "active"),
                    pause_reason=(
                        str(self.queue["pause_reason"]) if self.queue.get("pause_reason") else None
                    ),
                    width=width,
                )
            ]
        )

    def composer_prompt(self) -> FormattedText:
        queued_input: str | None = None
        queued_turns = self.queue.get("queued_turns")
        current_turn_ids = {
            str(turn.get("run_id") or turn.get("id") or "")
            for turn in (self.queue.get("active_turn"), self.queue.get("head_turn"))
            if isinstance(turn, dict)
        }
        if isinstance(queued_turns, list) and queued_turns:
            for next_turn in queued_turns:
                if not isinstance(next_turn, dict):
                    continue
                turn_id = str(next_turn.get("run_id") or next_turn.get("id") or "")
                if turn_id in current_turn_ids or turn_id == self._watch_run_id:
                    continue
                user_input = next_turn.get("user_input")
                if isinstance(user_input, str) and user_input.strip():
                    queued_input = user_input
                    break
        if queued_input is None:
            return _USER_PROMPT

        width = shutil.get_terminal_size((80, 24)).columns
        preview_width = max(
            1,
            min(
                _QUEUE_PREVIEW_MAX_CELLS,
                width - get_cwidth(_QUEUE_LABEL),
            ),
        )
        preview = _truncate_cells(" ".join(queued_input.split()), preview_width)
        return FormattedText(
            [
                ("class:nico.queue-label", _QUEUE_LABEL),
                ("class:nico.queue-preview", preview),
                ("", "\n"),
                *_USER_PROMPT,
            ]
        )

    async def decide_approval(self, approval: dict[str, Any], answer: str) -> bool:
        decision = {
            "1": ("approve", "once"),
            "2": ("approve", "run"),
            "3": ("reject", None),
        }.get(answer.strip())
        if decision is None:
            return False
        await self._api_call(
            lambda: self.client.decide_tool_approval(
                str(approval["id"]),
                expected_revision=int(approval["revision"]),
                decision=decision[0],
                allowed_scope=decision[1],
                idempotency_key=str(uuid4()),
            )
        )
        return True

    def save_draft(self, text: str, cursor_position: int) -> None:
        self._draft = Document(text, cursor_position=cursor_position)

    @property
    def saved_draft(self) -> Document | None:
        return self._draft

    async def close(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        watch_client = self._watch_client
        if watch_client is not None:
            try:
                watch_client.close()
            except Exception:
                pass
        if self._consumer_task is not None:
            self._consumer_task.cancel()
        tasks = list(self._watch_tasks)
        if tasks:
            try:
                await asyncio.wait(tasks, timeout=2)
            except Exception:
                pass
        if self._consumer_task is not None:
            await asyncio.gather(self._consumer_task, return_exceptions=True)

    async def _resolve_next_approval(self) -> bool:
        approval = self._approvals[0]
        self.runner.renderer.approval(approval)
        self._clear_activity()
        try:
            while True:
                try:
                    answer = await self.prompt_session.prompt_async(
                        _APPROVAL_PROMPT,
                        bottom_toolbar=self.footer,
                        refresh_interval=0.25,
                    )
                except KeyboardInterrupt:
                    await self.cancel_active_turn()
                    self._discard_current_approval()
                    return True
                except EOFError:
                    return False
                if await self.decide_approval(approval, answer):
                    self._discard_current_approval()
                    await self._refresh_state()
                    self._invalidate()
                    return True
                self.runner.output.out.print("[yellow]Choose 1, 2, or 3.[/yellow]")
        finally:
            self._sync_activity()

    async def _refresh_state(self, *, include_approvals: bool = False) -> None:
        def fetch() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None, list[dict]]:
            conversation = self.client.get_conversation(self.conversation["id"])
            queue = self.client.get_conversation_queue(self.conversation["id"])
            target = queue.get("active_turn") or queue.get("head_turn")
            runtime: dict[str, Any] | None = None
            if isinstance(target, dict) and target.get("run_id"):
                try:
                    runtime = self.client.get_runtime(str(target["run_id"]))
                except CliError as exc:
                    if exc.code != "RESOURCE_NOT_FOUND":
                        raise
            approvals: list[dict] = []
            if include_approvals and isinstance(target, dict) and target.get("run_id"):
                approvals = self.client.list_tool_approvals(
                    run_id=str(target["run_id"]), status="requested"
                )
            return conversation, queue, runtime, approvals

        conversation, queue, runtime, approvals = await self._api_call(fetch)
        self.conversation = self.runner._inherit_cli_context(conversation, self.conversation)
        self.queue = queue
        self.next_approval_mode = str(conversation.get("approval_mode") or "ask")
        manifest = (runtime or {}).get("execution_manifest") or {}
        policy = manifest.get("tool_approval_policy") or {}
        self.current_approval_mode = (
            str(policy["mode"]) if isinstance(policy, dict) and policy.get("mode") else None
        )
        if manifest.get("model"):
            self.model = str(manifest["model"])
        offered_approval = False
        for approval in approvals:
            offered_approval = self._offer_approval(approval) or offered_approval
        if offered_approval:
            self._interrupt_for_approval()
        self._invalidate()

    async def _api_call(self, operation: Callable[[], Any]) -> Any:
        async with self._api_lock:
            return await asyncio.to_thread(operation)

    async def _ensure_watch(self) -> None:
        target = self.queue.get("active_turn") or self.queue.get("head_turn")
        if not isinstance(target, dict) or not target.get("run_id"):
            return
        run_id = str(target["run_id"])
        if target.get("run_status") in _TERMINAL or self._watch_run_id == run_id:
            return
        if self._watch_run_id is not None and self._watch_run_id != run_id:
            self._stop_current_watch()
        self._watch_run_id = run_id
        initial = {
            "pending": "Queued",
            "planning": "Planning",
            "running": "Working",
            "waiting_for_tool": "Running tool",
            "waiting_for_approval": "Waiting for approval",
        }.get(str(target.get("run_status")), "Preparing")
        self.progress = self.runner.renderer.progress(initial=initial)
        self._sync_activity()
        task = asyncio.create_task(
            asyncio.to_thread(
                self._watch_blocking,
                run_id,
                str(target["id"]),
                str(self.conversation["id"]),
            ),
            name=f"nico-chat-watch-{run_id}",
        )
        self._watch_tasks.add(task)
        task.add_done_callback(self._watch_tasks.discard)

    def _watch_blocking(self, run_id: str, turn_id: str, conversation_id: str) -> None:
        client = self.client_factory()
        self._watch_client = client
        try:
            if self._stop.is_set():
                return
            for event in client.stream_run_events(
                run_id,
                on_connection=lambda state, attempt, maximum: self._enqueue_from_thread(
                    "connection", (run_id, state, attempt, maximum)
                ),
            ):
                if self._stop.is_set():
                    return
                self._enqueue_from_thread("event", (run_id, event))
            if self._stop.is_set():
                return
            turn = client.get_conversation_turn(turn_id)
            queue = client.get_conversation_queue(conversation_id)
            self._enqueue_from_thread("finished", (turn, queue, run_id))
        except Exception as exc:
            if not self._stop.is_set():
                error = (
                    exc
                    if isinstance(exc, CliError)
                    else CliError(
                        "CHAT_WATCH_FAILED",
                        "background Run observation failed",
                    )
                )
                self._enqueue_from_thread("error", (run_id, error))
        finally:
            try:
                client.close()
            except Exception:
                pass
            if self._watch_client is client:
                self._watch_client = None

    def _enqueue_from_thread(self, kind: str, value: Any) -> None:
        loop = self._loop
        if loop is None or self._stop.is_set() or loop.is_closed():
            return
        future = asyncio.run_coroutine_threadsafe(self._background.put((kind, value)), loop)
        try:
            future.result(timeout=5)
        except Exception:
            future.cancel()

    async def _consume_background(self) -> None:
        while True:
            kind, value = await self._background.get()
            if kind == "event":
                run_id, event = value
                if run_id != self._watch_run_id:
                    continue
                if delta := execution_event_output_delta(event):
                    self._append_stream_delta(delta)
                if execution_event_has_durable_output(event):
                    await run_in_terminal(lambda event=event: self.progress.event(event))
                else:
                    self.progress.event(event)
                self._sync_activity()
                if event.get("type") == "ApprovalRequested":
                    approval_id = (event.get("payload") or {}).get("approval_id")
                    if approval_id and str(approval_id) not in self._seen_approvals:
                        try:
                            approval = await self._fetch_approval(str(approval_id))
                        except CliError as exc:
                            await run_in_terminal(lambda exc=exc: self.runner.output.error(exc))
                        else:
                            if self._offer_approval(approval):
                                self._interrupt_for_approval()
            elif kind == "connection":
                run_id, state, attempt, maximum = value
                if run_id != self._watch_run_id:
                    continue
                self.progress.connection(state, attempt, maximum)
                self._sync_activity()
            elif kind == "finished":
                turn, queue, run_id = value
                if run_id != self._watch_run_id:
                    continue
                self.queue = queue
                self._watch_run_id = None
                self._clear_stream()
                self._sync_activity()
                await run_in_terminal(self.progress.stop)
                turn_id = str(turn.get("id") or "")
                if turn_id and turn_id not in self._rendered_turns:
                    self._rendered_turns.add(turn_id)
                    await run_in_terminal(
                        lambda turn=turn: self.runner._render_final(
                            self.conversation,
                            turn,
                            show_metrics=self.show_response_metrics,
                        )
                    )
                try:
                    await self._refresh_state(include_approvals=True)
                except CliError as exc:
                    await run_in_terminal(lambda exc=exc: self.runner.output.error(exc))
                await self._ensure_watch()
            elif kind == "error":
                run_id, error = value
                if run_id != self._watch_run_id:
                    continue
                self._watch_run_id = None
                self._clear_stream()
                self._sync_activity()
                await run_in_terminal(lambda error=error: self.runner.output.error(error))
            self._invalidate()

    def _append_stream_delta(self, delta: str) -> None:
        append = getattr(self.prompt_session, "append_stream_delta", None)
        if callable(append):
            append(delta)

    def _clear_stream(self) -> None:
        clear = getattr(self.prompt_session, "clear_stream", None)
        if callable(clear):
            clear()

    def _sync_activity(self) -> None:
        provider_setter = getattr(self.prompt_session, "set_activity_provider", None)
        if callable(provider_setter):
            provider_setter(self._activity_status if self._watch_run_id is not None else None)
            return
        setter = getattr(self.prompt_session, "set_activity", None)
        if not callable(setter):
            return
        if self._watch_run_id is None:
            setter(None)
            return
        setter(self._activity_status())

    def _clear_activity(self) -> None:
        provider_setter = getattr(self.prompt_session, "set_activity_provider", None)
        if callable(provider_setter):
            provider_setter(None)
            return
        setter = getattr(self.prompt_session, "set_activity", None)
        if callable(setter):
            setter(None)

    def _activity_status(self) -> str:
        width = max(1, shutil.get_terminal_size((80, 24)).columns - 4)
        return self.progress.status_text(width)

    async def _fetch_approval(self, approval_id: str) -> dict[str, Any]:
        last_error: CliError | None = None
        for attempt in range(_APPROVAL_FETCH_ATTEMPTS):
            try:
                return await self._api_call(lambda: self.client.get_tool_approval(approval_id))
            except CliError as exc:
                last_error = exc
                if attempt + 1 < _APPROVAL_FETCH_ATTEMPTS:
                    await asyncio.sleep(_APPROVAL_FETCH_RETRY_SECONDS * (2**attempt))
        assert last_error is not None
        raise last_error

    def _offer_approval(self, approval: dict[str, Any]) -> bool:
        approval_id = str(approval.get("id") or "")
        if not approval_id or approval_id in self._seen_approvals:
            return False
        self._seen_approvals.add(approval_id)
        self._approvals.append(approval)
        self._approval_ready.set()
        return True

    def _discard_current_approval(self) -> None:
        self._approvals.popleft()
        if not self._approvals:
            self._approval_ready.clear()

    async def _interrupt_composer_on_approval(self) -> None:
        await self._approval_ready.wait()
        while self._approvals and not self._stop.is_set():
            if self.prompt_session.app.is_running:
                self._interrupt_for_approval()
                return
            await asyncio.sleep(0.01)

    def _stop_current_watch(self) -> None:
        self._watch_run_id = None
        client = self._watch_client
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def _interrupt_for_approval(self) -> None:
        app = self.prompt_session.app
        if not app.is_running:
            return
        buffer = app.current_buffer
        self.save_draft(buffer.text, buffer.cursor_position)
        try:
            app.exit(result=_APPROVAL_INTERRUPT)
        except Exception:
            pass

    def _invalidate(self) -> None:
        try:
            app = self.prompt_session.app
            if app.is_running:
                app.invalidate()
        except Exception:
            pass


def _truncate_cells(value: str, limit: int) -> str:
    if sum(get_cwidth(character) for character in value) <= limit:
        return value
    if limit <= 1:
        return "…"
    remaining = limit - get_cwidth("…")
    kept: list[str] = []
    for character in value:
        width = get_cwidth(character)
        if width > remaining:
            break
        kept.append(character)
        remaining -= width
    return "".join(kept).rstrip() + "…"
