from __future__ import annotations

import json
from io import StringIO

from nico_agent.cli.errors import CliError
from nico_agent.cli.output import Output


def test_json_output_is_parseable_and_has_no_ansi() -> None:
    stdout = StringIO()
    output = Output(json_mode=True, stdout=stdout)

    output.emit({"status": "ready", "message": "你好"})

    assert json.loads(stdout.getvalue()) == {"status": "ready", "message": "你好"}
    assert "\x1b[" not in stdout.getvalue()


def test_json_error_is_written_to_stderr() -> None:
    stderr = StringIO()
    output = Output(json_mode=True, stderr=stderr)

    output.error(CliError("FAILED", "safe", request_id="request-1"))

    assert json.loads(stderr.getvalue()) == {
        "error": {"code": "FAILED", "message": "safe", "request_id": "request-1"}
    }


def test_no_color_human_table_contains_no_ansi() -> None:
    stdout = StringIO()
    output = Output(no_color=True, stdout=stdout)

    output.table([{"id": "a1", "status": "ready"}], title="Agents")

    assert "Agents" in stdout.getvalue()
    assert "a1" in stdout.getvalue()
    assert "\x1b[" not in stdout.getvalue()
