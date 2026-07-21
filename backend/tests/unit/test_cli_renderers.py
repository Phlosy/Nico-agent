from __future__ import annotations

from contextlib import contextmanager
from io import StringIO

from nico_agent.cli.output import Output
from nico_agent.cli.renderers import ExecutionRenderer, ProjectRenderer, ProviderSetupRenderer


def test_human_renderer_snapshot_has_header_events_and_inspection_views() -> None:
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
    renderer.event(
        {"sequence": 2, "type": "ToolCallStarted", "payload": {"tool_name": "http_read"}}
    )
    renderer.event({"sequence": 3, "type": "RuntimeOutputDelta", "payload": {"message": "stream"}})
    renderer.event({"sequence": 4, "type": "RunCompleted", "payload": {}})
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
    assert "Nico Agent" in rendered
    assert "Researcher" in rendered
    assert "ToolCallStarted" in rendered
    assert "stream\n✓ RunCompleted" in rendered
    assert "Plan Revisions" in rendered
    assert "Run Steps" in rendered
    assert "Tool Calls" in rendered
    assert "Artifacts" in rendered
    assert "Usage" in rendered
    assert "\x1b[" not in rendered


def test_json_mode_never_renders_header_or_events() -> None:
    stdout = StringIO()
    renderer = ExecutionRenderer(
        Output(json_mode=True, no_color=False, stdout=stdout, stderr=StringIO())
    )

    renderer.header({"agent": "hidden"})
    renderer.event({"sequence": 1, "type": "RunStarted", "payload": {}})

    assert stdout.getvalue() == ""


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
