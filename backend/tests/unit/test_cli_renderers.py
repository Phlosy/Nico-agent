from __future__ import annotations

from io import StringIO

from nico_agent.cli.output import Output
from nico_agent.cli.renderers import ExecutionRenderer


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
