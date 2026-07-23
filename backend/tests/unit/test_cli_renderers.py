from __future__ import annotations

from contextlib import contextmanager
from io import StringIO

from prompt_toolkit.utils import get_cwidth
from rich.console import Console

from nico_agent.cli.output import Output
from nico_agent.cli.renderers import (
    ExecutionRenderer,
    ProjectRenderer,
    ProviderSetupRenderer,
    chat_footer_status,
    execution_event_has_durable_output,
    execution_event_output_delta,
)


def test_human_renderer_shows_user_actions_without_internal_lifecycle_events() -> None:
    stdout = StringIO()
    renderer = ExecutionRenderer(
        Output(json_mode=False, no_color=True, stdout=stdout, stderr=StringIO())
    )

    renderer.header(
        {
            "agent": "Researcher",
            "version": 3,
            "runtime": "native",
            "project": "Alpha Lab",
            "tools": ["http_read", "python_sandbox"],
        }
    )
    renderer.event({"sequence": 1, "type": "TaskCreated", "payload": {}})
    renderer.event({"sequence": 2, "type": "RuntimeStepStarted", "payload": {}})
    renderer.event(
        {
            "sequence": 3,
            "type": "RuntimeModelOutputDelta",
            "payload": {"message": "private intermediate output"},
        }
    )
    renderer.event({"sequence": 4, "type": "ToolCallStarted", "payload": {"tool": "http_read@1"}})
    renderer.event({"sequence": 5, "type": "ToolCallSucceeded", "payload": {"tool": "http_read@1"}})
    renderer.event(
        {
            "sequence": 6,
            "type": "ArtifactAvailable",
            "payload": {"name": "report.md"},
        }
    )
    renderer.event({"sequence": 7, "type": "RunCompleted", "payload": {}})
    renderer.final({"assistant_output": {"answer": "Final answer"}})
    renderer.plans(
        [{"revision": 1, "status": "active", "objective": "Research", "reason": "initial"}]
    )
    renderer.run_steps([{"sequence": 1, "kind": "model", "status": "completed"}])
    renderer.tools(
        [{"tool_name": "http_read", "tool_version": "1", "status": "completed", "usage": {}}]
    )
    renderer.artifacts(
        [{"id": "a1", "name": "report.md", "artifact_type": "report", "status": "available"}]
    )
    renderer.usage({"usage": {"input_tokens": 12, "output_tokens": 4}}, {"cost": {}})

    rendered = stdout.getvalue()
    assert "Nico" in rendered
    assert "Researcher" in rendered
    assert "Running http_read@1" not in rendered
    assert "✓ http_read@1" in rendered
    assert "Saved report.md" in rendered
    assert "Final answer" in rendered
    assert "TaskCreated" not in rendered
    assert "RuntimeStepStarted" not in rendered
    assert "RuntimeModelOutputDelta" not in rendered
    assert "private intermediate output" not in rendered
    assert "RunCompleted" not in rendered
    assert "#1" not in rendered
    assert "Plan Revisions" in rendered
    assert "Run Steps" in rendered
    assert "Tool Calls" in rendered
    assert "Artifacts" in rendered
    assert "Usage" in rendered
    assert "\x1b[" not in rendered


def test_chat_header_and_answer_use_a_lightweight_responsive_conversation_layout() -> None:
    stdout = StringIO()
    output = Output(json_mode=False, no_color=True, stdout=stdout, stderr=StringIO())
    output.out = Console(
        file=stdout,
        width=44,
        color_system=None,
        no_color=True,
        highlight=False,
        soft_wrap=False,
    )
    renderer = ExecutionRenderer(output)

    renderer.header(
        {
            "agent": "Researcher",
            "version": 3,
            "runtime": "nico_native",
            "model": "deepseek-v4-pro",
            "project": "Alpha Lab",
            "tools": ["web.search@1.0.0", "web.fetch@1.0.0"],
        }
    )
    renderer.final(
        {"assistant_output": {"answer": "A concise answer.\n\n- First source\n- Second source"}}
    )

    rendered = stdout.getvalue()
    lines = rendered.splitlines()
    assert " /\\_/\\" in rendered
    assert "Nico" in rendered
    assert "Researcher · v3" in rendered
    assert "deepseek-v4-pro · nico_native" in rendered
    assert "Alpha Lab · 2 tools" in rendered
    assert "Nico" in rendered
    assert "A concise answer." in rendered
    assert "╭" in rendered
    assert "╰" in rendered
    assert all(len(line) <= 44 for line in lines)


def test_chat_answer_optionally_ends_with_dim_elapsed_and_token_usage() -> None:
    stdout = StringIO()
    renderer = ExecutionRenderer(
        Output(json_mode=False, no_color=True, stdout=stdout, stderr=StringIO())
    )
    turn = {
        "assistant_output": {"answer": "Measured answer"},
        "usage": {"input_tokens": 30, "output_tokens": 12, "total_tokens": 42},
        "created_at": "2026-07-23T08:00:00Z",
        "updated_at": "2026-07-23T08:00:01.540Z",
    }

    renderer.final(turn)
    without_metrics = stdout.getvalue()
    stdout.seek(0)
    stdout.truncate()
    renderer.final(turn, show_metrics=True)
    with_metrics = stdout.getvalue()

    assert "Measured answer" in without_metrics
    assert "42 tokens" not in without_metrics
    assert "1.5s · 42 tokens" in with_metrics
    assert with_metrics.rfind("1.5s · 42 tokens") > with_metrics.find("Measured answer")


def test_chat_answer_metrics_report_unavailable_usage_without_exposing_raw_fields() -> None:
    stdout = StringIO()
    renderer = ExecutionRenderer(
        Output(json_mode=False, no_color=True, stdout=stdout, stderr=StringIO())
    )

    renderer.final(
        {
            "assistant_output": {"answer": "Answer without provider accounting"},
            "usage": {"status": "missing", "private_provider_field": "do-not-render"},
            "created_at": "2026-07-23T08:00:00Z",
            "updated_at": "2026-07-23T08:00:02Z",
        },
        show_metrics=True,
    )

    rendered = stdout.getvalue()
    assert "2.0s · tokens unavailable" in rendered
    assert "private_provider_field" not in rendered
    assert "do-not-render" not in rendered


def test_chat_answer_metrics_do_not_report_partial_usage_as_total_tokens() -> None:
    stdout = StringIO()
    renderer = ExecutionRenderer(
        Output(json_mode=False, no_color=True, stdout=stdout, stderr=StringIO())
    )

    renderer.final(
        {
            "assistant_output": {"answer": "Answer with partial accounting"},
            "usage": {"input_tokens": 30},
            "created_at": "2026-07-23T08:00:00Z",
            "updated_at": "2026-07-23T08:00:02Z",
        },
        show_metrics=True,
    )

    rendered = stdout.getvalue()
    assert "2.0s · tokens unavailable" in rendered
    assert "30 tokens" not in rendered


def test_chat_answer_metrics_sum_complete_input_and_output_usage() -> None:
    stdout = StringIO()
    renderer = ExecutionRenderer(
        Output(json_mode=False, no_color=True, stdout=stdout, stderr=StringIO())
    )

    renderer.final(
        {
            "assistant_output": {"answer": "Answer with split accounting"},
            "usage": {"input_tokens": 30, "output_tokens": 12},
            "created_at": "2026-07-23T08:00:00Z",
            "updated_at": "2026-07-23T08:00:02Z",
        },
        show_metrics=True,
    )

    assert "2.0s · 42 tokens" in stdout.getvalue()


def test_chat_answer_metrics_treat_mixed_timezone_timestamps_as_unavailable() -> None:
    stdout = StringIO()
    renderer = ExecutionRenderer(
        Output(json_mode=False, no_color=True, stdout=stdout, stderr=StringIO())
    )

    renderer.final(
        {
            "assistant_output": {"answer": "Answer with inconsistent timestamps"},
            "usage": {"total_tokens": 42},
            "created_at": "2026-07-23T08:00:00",
            "updated_at": "2026-07-23T08:00:02Z",
        },
        show_metrics=True,
    )

    rendered = stdout.getvalue()
    assert "Answer with inconsistent timestamps" in rendered
    assert "time unavailable · 42 tokens" in rendered


def test_chat_answer_metrics_use_dim_terminal_styling() -> None:
    stdout = StringIO()
    output = Output(json_mode=False, no_color=False, stdout=stdout, stderr=StringIO())
    output.out = Console(
        file=stdout,
        force_terminal=True,
        color_system="truecolor",
        highlight=False,
    )
    renderer = ExecutionRenderer(output)

    renderer.final(
        {
            "assistant_output": {"answer": "Primary answer"},
            "usage": {"total_tokens": 1},
            "created_at": "2026-07-23T08:00:00Z",
            "updated_at": "2026-07-23T08:00:01Z",
        },
        show_metrics=True,
    )

    rendered = stdout.getvalue()
    assert "\x1b[2m1.0s · 1 token\x1b[0m" in rendered


def test_chat_user_message_keeps_a_distinct_identity_in_scrollback() -> None:
    stdout = StringIO()
    renderer = ExecutionRenderer(
        Output(json_mode=False, no_color=True, stdout=stdout, stderr=StringIO())
    )

    renderer.user_message("排队的问题")

    assert stdout.getvalue() == "you › 排队的问题\n"


def test_json_mode_never_renders_header_or_events() -> None:
    stdout = StringIO()
    renderer = ExecutionRenderer(
        Output(json_mode=True, no_color=False, stdout=stdout, stderr=StringIO())
    )

    renderer.header({"agent": "hidden"})
    renderer.event({"sequence": 1, "type": "RunStarted", "payload": {}})

    assert stdout.getvalue() == ""


def test_execution_progress_projects_phases_without_leaking_event_payloads() -> None:
    stdout = StringIO()
    now = [10.0]
    renderer = ExecutionRenderer(
        Output(json_mode=False, no_color=True, stdout=stdout, stderr=StringIO())
    )
    progress = renderer.progress(clock=lambda: now[0])

    with progress:
        assert progress.activity == "Queued"
        progress.event({"type": "RunPlanningStarted", "payload": {"objective": "secret"}})
        assert progress.activity == "Planning"
        progress.event(
            {
                "type": "RuntimeModelOutputDelta",
                "payload": {"message": "private intermediate output"},
            }
        )
        assert progress.activity == "Thinking"
        progress.event(
            {
                "type": "RuntimeDelegationAccepted",
                "payload": {"child_run_id": "internal-child"},
            }
        )
        assert progress.activity == "Waiting for subagent"
        progress.event({"type": "RuntimeOutputDelta", "payload": {"content": "draft"}})
        assert progress.activity == "Finalizing"
        progress.event(
            {
                "type": "ToolCallStarted",
                "payload": {"tool": {"arguments": "nested secret"}},
            }
        )
        assert progress.activity == "Running tool"
        assert "nested secret" not in progress.status_text(80)

    rendered = stdout.getvalue()
    assert "› Queued" in rendered
    assert "secret" not in rendered
    assert "private intermediate output" not in rendered
    assert "internal-child" not in rendered
    assert "draft" not in rendered
    assert "nested secret" not in rendered


def test_execution_renderer_rejects_non_string_durable_labels() -> None:
    stdout = StringIO()
    renderer = ExecutionRenderer(
        Output(json_mode=False, no_color=True, stdout=stdout, stderr=StringIO())
    )

    renderer.event(
        {
            "type": "ArtifactAvailable",
            "payload": {"name": {"arguments": "private artifact payload"}},
        }
    )

    rendered = stdout.getvalue()
    assert "Saved artifact" in rendered
    assert "private artifact payload" not in rendered


def test_execution_progress_keeps_one_durable_tool_result_with_correlated_duration() -> None:
    stdout = StringIO()
    now = [20.0]
    renderer = ExecutionRenderer(
        Output(json_mode=False, no_color=True, stdout=stdout, stderr=StringIO())
    )

    with renderer.progress(clock=lambda: now[0]) as progress:
        progress.event(
            {
                "type": "ToolCallStarted",
                "payload": {"run_step_id": "step-1", "tool": "http_read@1"},
            }
        )
        assert progress.activity == "Running http_read@1"
        assert "Running http_read@1" not in stdout.getvalue()
        now[0] = 22.36
        progress.event(
            {
                "type": "ToolCallSucceeded",
                "payload": {
                    "run_step_id": "step-1",
                    "tool": "http_read@1",
                    "result": {"private": True},
                },
            }
        )
        assert progress.activity == "Continuing"

    rendered = stdout.getvalue()
    assert rendered.count("http_read@1") == 1
    assert "✓ http_read@1 (2.4s)" in rendered
    assert "private" not in rendered


def test_web_progress_is_semantic_and_omits_query_url_and_result() -> None:
    stdout = StringIO()
    now = [20.0]
    renderer = ExecutionRenderer(
        Output(json_mode=False, no_color=True, stdout=stdout, stderr=StringIO())
    )

    with renderer.progress(clock=lambda: now[0]) as progress:
        progress.event(
            {
                "type": "ToolCallStarted",
                "payload": {
                    "tool_call_id": "search-1",
                    "tool": "web.search@1.0.0",
                    "arguments": {"query": "private research query"},
                },
            }
        )
        assert progress.activity == "Running Web search"
        now[0] = 21.2
        progress.event(
            {
                "type": "ToolCallSucceeded",
                "payload": {
                    "tool_call_id": "search-1",
                    "tool": "web.search@1.0.0",
                    "result": {"url": "https://secret.example/path?token=private"},
                },
            }
        )

    rendered = stdout.getvalue()
    assert "✓ Web research · 1 search" in rendered
    assert "Web search (1.2s)" not in rendered
    assert "private research query" not in rendered
    assert "secret.example" not in rendered
    assert "token=private" not in rendered


def test_tool_progress_uses_friendly_builtin_activity_labels() -> None:
    renderer = ExecutionRenderer(
        Output(json_mode=False, no_color=True, stdout=StringIO(), stderr=StringIO())
    )
    progress = renderer.progress(clock=lambda: 20.0)

    progress.event(
        {
            "type": "ToolCallStarted",
            "payload": {"tool_call_id": "read-1", "tool": "file.read@1.0.0"},
        }
    )
    assert progress.activity == "Running Reading file"

    progress.event(
        {
            "type": "ToolCallStarted",
            "payload": {"tool_call_id": "write-1", "tool": "file.write@1.0.0"},
        }
    )
    assert progress.activity == "Running Writing file"

    progress.event(
        {
            "type": "ToolCallStarted",
            "payload": {"tool_call_id": "python-1", "tool": "python.execute@1.0.0"},
        }
    )
    assert progress.activity == "Running Python"


def test_web_progress_summarizes_recovered_source_failures_once() -> None:
    stdout = StringIO()
    renderer = ExecutionRenderer(
        Output(json_mode=False, no_color=True, stdout=stdout, stderr=StringIO())
    )

    with renderer.progress() as progress:
        for event_type, tool, code in (
            ("ToolCallSucceeded", "web.search@1.0.0", None),
            ("ToolCallSucceeded", "web.fetch@1.1.0", None),
            ("ToolCallFailed", "web.fetch@1.1.0", "WEB_FETCH_SOURCE_DENIED"),
            ("ToolCallFailed", "web.fetch@1.1.0", "WEB_FETCH_UNAVAILABLE"),
        ):
            progress.event(
                {
                    "type": event_type,
                    "payload": {
                        "tool_call_id": f"call-{tool}-{code}",
                        "tool": tool,
                        "code": code,
                    },
                }
            )

    rendered = stdout.getvalue()
    assert "✓ Web research · 1 search · 1 source read · 2 skipped" in rendered
    assert "WEB_FETCH_SOURCE_DENIED" not in rendered
    assert "WEB_FETCH_UNAVAILABLE" not in rendered
    assert rendered.count("Web research") == 1


def test_web_approval_shows_only_bounded_query_or_origin() -> None:
    stdout = StringIO()
    renderer = ExecutionRenderer(
        Output(json_mode=False, no_color=True, stdout=stdout, stderr=StringIO())
    )
    query = "q" * 400

    renderer.approval(
        {
            "tool_name": "web.search",
            "tool_version": "1.0.0",
            "arguments": {"query": query, "domains": ["private.example"]},
        }
    )
    renderer.approval(
        {
            "tool_name": "web.fetch",
            "tool_version": "1.0.0",
            "arguments": {
                "url": "https://source.example/private/path?secret=canary",
                "search_tool_call_id": "internal-call-id",
            },
        }
    )

    rendered = stdout.getvalue()
    assert rendered.count("Approval required") == 2
    assert "q" * 300 not in rendered
    assert rendered.count("q") > 50
    assert "…" in rendered
    assert "private.example" not in rendered
    assert "https://source.example" in rendered
    assert "/private/path" not in rendered
    assert "secret=canary" not in rendered
    assert "internal-call-id" not in rendered


def test_execution_progress_omits_guessed_duration_and_deduplicates_non_tty_reconnects() -> None:
    stdout = StringIO()
    renderer = ExecutionRenderer(
        Output(json_mode=False, no_color=True, stdout=stdout, stderr=StringIO())
    )

    with renderer.progress() as progress:
        progress.connection("reconnecting", attempt=1, max_attempts=3)
        progress.connection("reconnecting", attempt=1, max_attempts=3)
        progress.connection("recovered")
        progress.event(
            {
                "type": "ToolCallFailed",
                "payload": {
                    "run_step_id": "missing-start",
                    "tool": "python_sandbox@1",
                    "code": "EXECUTION_FAILED",
                    "error": {"message": "private traceback"},
                },
            }
        )

    rendered = stdout.getvalue()
    assert rendered.count("Reconnecting 1/3") == 1
    assert "Tool failed: python_sandbox@1 (EXECUTION_FAILED)" in rendered
    assert "private traceback" not in rendered
    assert "0.0s" not in rendered


def test_execution_progress_status_is_width_bounded_and_prioritizes_connection() -> None:
    renderer = ExecutionRenderer(
        Output(json_mode=False, no_color=True, stdout=StringIO(), stderr=StringIO())
    )
    progress = renderer.progress(clock=lambda: 65.0)
    progress.event(
        {
            "type": "ToolCallStarted",
            "payload": {
                "run_step_id": "step-1",
                "tool": "a_very_long_tool_reference@123",
            },
        }
    )
    progress.connection("reconnecting", attempt=2, max_attempts=3)

    wide = progress.status_text(80)
    medium = progress.status_text(40)
    narrow = progress.status_text(20)

    assert "a_very_long_tool_reference@123" in wide
    assert "reconnecting 2/3" in wide
    assert "Running" in medium
    assert "reconnecting 2/3" in medium
    assert "a_very_long_tool_reference@123" not in medium
    assert len(wide) <= 80
    assert len(medium) <= 40
    assert len(narrow) <= 20
    assert "Running" in narrow


def test_chat_footer_is_bounded_and_keeps_live_activity_below_the_composer() -> None:
    wide = chat_footer_status(
        model="deepseek-v4-pro-with-a-very-long-provider-prefix",
        current_approval_mode="ask",
        next_approval_mode="auto-all",
        activity="Finalizing · 1:21",
        queued_count=2,
        queue_state="active",
        pause_reason=None,
        width=160,
    )
    narrow = chat_footer_status(
        model="deepseek-v4-pro-with-a-very-long-provider-prefix",
        current_approval_mode="ask",
        next_approval_mode="auto-all",
        activity="Finalizing · 1:21",
        queued_count=2,
        queue_state="paused",
        pause_reason="run_failed",
        width=36,
    )

    assert "deepseek-v4-pro" in wide
    assert "ask → auto-all" in wide
    assert "Finalizing · 1:21" in wide
    assert "queued 2" in wide
    assert "│" in wide
    assert len(narrow) <= 36
    assert "paused" in narrow

    cjk = chat_footer_status(
        model="深度求索模型版本",
        current_approval_mode="ask",
        next_approval_mode="ask",
        activity="Thinking · 0:08",
        queued_count=0,
        queue_state="active",
        pause_reason=None,
        width=20,
    )
    assert sum(get_cwidth(character) for character in cjk) <= 20


def test_chat_footer_omits_an_empty_queue_without_hiding_session_status() -> None:
    footer = chat_footer_status(
        model="deepseek-v4-pro",
        current_approval_mode="ask",
        next_approval_mode="ask",
        activity="Finalizing · 1:21",
        queued_count=0,
        queue_state="active",
        pause_reason=None,
        width=100,
    )

    assert footer == "model deepseek-v4-pro  │  permission ask  │  Finalizing · 1:21"
    assert "queue" not in footer
    assert "queued" not in footer


def test_only_curated_durable_events_interrupt_the_interactive_composer() -> None:
    assert not execution_event_has_durable_output(
        {"type": "RuntimeModelOutputDelta", "payload": {"message": "private"}}
    )
    assert not execution_event_has_durable_output(
        {"type": "ToolCallStarted", "payload": {"tool": "web.search@1.0.0"}}
    )
    assert not execution_event_has_durable_output(
        {"type": "ToolCallSucceeded", "payload": {"tool": "web.search@1.0.0"}}
    )
    assert execution_event_has_durable_output(
        {"type": "ToolCallSucceeded", "payload": {"tool": "file.read@1.0.0"}}
    )
    assert execution_event_has_durable_output(
        {
            "type": "ToolCallRejected",
            "payload": {"tool": "web.fetch@1.1.0", "code": "TOOL_APPROVAL_REJECTED"},
        }
    )
    assert execution_event_has_durable_output(
        {"type": "ArtifactAvailable", "payload": {"name": "report.md"}}
    )
    assert execution_event_has_durable_output({"type": "RunFailed", "payload": {}})


def test_only_user_visible_runtime_deltas_are_streamed() -> None:
    assert (
        execution_event_output_delta(
            {
                "type": "RuntimeModelOutputDelta",
                "payload": {
                    "message": "你",
                    "payload": {
                        "call_key": "model:1",
                        "delta": "你",
                        "visibility": "assistant",
                    },
                },
            }
        )
        == "你"
    )
    assert (
        execution_event_output_delta(
            {
                "type": "RuntimeModelOutputDelta",
                "payload": {
                    "message": "private",
                    "payload": {
                        "call_key": "reflection:1",
                        "delta": "private",
                        "visibility": "internal",
                    },
                },
            }
        )
        is None
    )
    assert (
        execution_event_output_delta(
            {
                "type": "RuntimeOutputDelta",
                "payload": {"message": "Hermes answer", "visibility": "assistant"},
            }
        )
        == "Hermes answer\n"
    )
    assert (
        execution_event_output_delta(
            {
                "type": "RuntimeOutputDelta",
                "payload": {"message": "mock step", "visibility": "internal"},
            }
        )
        is None
    )
    assert (
        execution_event_output_delta(
            {"type": "RuntimeModelCallStarted", "payload": {"message": "hidden"}}
        )
        is None
    )


def test_execution_progress_pause_resume_and_stop_are_idempotent() -> None:
    renderer = ExecutionRenderer(
        Output(json_mode=False, no_color=True, stdout=StringIO(), stderr=StringIO())
    )
    progress = renderer.progress()

    progress.start()
    assert progress.running is True
    progress.pause()
    assert progress.running is False
    progress.resume()
    assert progress.running is True
    assert progress.activity == "Continuing"
    progress.stop()
    progress.stop()
    assert progress.running is False


def test_human_renderer_surfaces_failures_and_cancellation_without_protocol_names() -> None:
    stdout = StringIO()
    renderer = ExecutionRenderer(
        Output(json_mode=False, no_color=True, stdout=stdout, stderr=StringIO())
    )

    renderer.event(
        {
            "sequence": 8,
            "type": "ToolCallFailed",
            "payload": {"tool": "python_sandbox@1", "code": "EXECUTION_FAILED"},
        }
    )
    renderer.event(
        {
            "sequence": 9,
            "type": "RunCancelled",
            "payload": {"reason": "private arbitrary cancellation reason"},
        }
    )
    renderer.event(
        {
            "sequence": 10,
            "type": "RunTimedOut",
            "payload": {"code": "RUN_TIMEOUT"},
        }
    )

    rendered = stdout.getvalue()
    assert "Tool failed: python_sandbox@1 (EXECUTION_FAILED)" in rendered
    assert "Run cancelled" in rendered
    assert "Run timed out: RUN_TIMEOUT" in rendered
    assert "private arbitrary cancellation reason" not in rendered
    assert "ToolCallFailed" not in rendered
    assert "RunCancelled" not in rendered
    assert "#8" not in rendered


def test_provider_setup_renderer_presents_guided_presets_and_other_provider() -> None:
    stdout = StringIO()
    renderer = ProviderSetupRenderer(
        Output(json_mode=False, no_color=True, stdout=stdout, stderr=StringIO())
    )

    renderer.welcome()
    renderer.providers(
        [
            {
                "key": "openai",
                "display_name": "OpenAI",
                "protocol": "openai_compatible",
                "recommended_models": ["gpt-5.6-terra", "gpt-5.6-sol"],
            }
        ]
    )
    renderer.models(["gpt-5.6-terra", "gpt-5.6-sol"], {"gpt-5.6-terra"})

    rendered = stdout.getvalue()
    assert "Connect a model Provider" in rendered
    assert "OpenAI" in rendered
    assert "OpenAI / local" in rendered
    assert "Other Provider" in rendered
    assert "Recommended" in rendered


def test_provider_progress_uses_a_terminal_spinner_without_rendering_markup(monkeypatch) -> None:
    output = Output(json_mode=False, no_color=True, stdout=StringIO(), stderr=StringIO())
    renderer = ProviderSetupRenderer(output)
    observed: list[tuple[str, str]] = []

    @contextmanager
    def status(message, *, spinner):
        observed.append((str(message), spinner))
        yield

    monkeypatch.setattr(output.out, "status", status)

    with renderer.progress("Verifying model [untrusted]…"):
        pass

    assert observed == [("Verifying model [untrusted]…", "dots")]


def test_project_renderer_separates_facts_from_narrative_and_excludes_reasoning() -> None:
    stdout = StringIO()
    renderer = ProjectRenderer(
        Output(json_mode=False, no_color=True, stdout=stdout, stderr=StringIO())
    )

    renderer.timeline(
        [
            {
                "sequence": 8,
                "kind": "tool",
                "event_type": "ToolCallCompleted",
                "facts": {"status": "completed", "tool": "tests"},
                "links": {"run": "/api/v1/runs/run-1"},
            }
        ]
    )
    renderer.cycles(
        [
            {
                "status": "completed",
                "trigger": "manual",
                "scheduled_for": "2026-07-21T00:00:00Z",
                "metrics": {"completed_tasks": 2},
                "narrative_summary": None,
            }
        ]
    )

    rendered = stdout.getvalue()
    assert "Auditable facts" in rendered
    assert "private model" in rendered
    assert "reasoning" in rendered
    assert "Database facts" in rendered
    assert "Lead narrative" in rendered
    assert "No model" in rendered
    assert "narrative" in rendered
