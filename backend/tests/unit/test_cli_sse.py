from __future__ import annotations

import pytest

from nico_agent.cli.errors import CliError
from nico_agent.cli.sse import SseParser


def test_parser_handles_fragmented_crlf_comments_and_multiline_data() -> None:
    parser = SseParser()

    assert parser.feed(": keep") == []
    assert parser.feed("-alive\r\nid: 7\r\nevent: Run") == []
    events = parser.feed('Completed\r\ndata: {"sequence": 7,\r\ndata: "ok": true}\r\n\r\n')

    assert len(events) == 1
    assert events[0].id == "7"
    assert events[0].event == "RunCompleted"
    assert events[0].data == '{"sequence": 7,\n"ok": true}'
    assert events[0].json() == {"sequence": 7, "ok": True}


def test_parser_ignores_empty_keepalives_and_flushes_final_event() -> None:
    parser = SseParser()
    assert parser.feed(": ping\n\n") == []
    assert parser.feed('id: 2\ndata: {"sequence": 2}') == []
    assert parser.finish()[0].json()["sequence"] == 2


def test_sse_event_rejects_non_object_json() -> None:
    parser = SseParser()
    event = parser.feed("data: []\n\n")[0]
    with pytest.raises(CliError, match="not an object"):
        event.json()
