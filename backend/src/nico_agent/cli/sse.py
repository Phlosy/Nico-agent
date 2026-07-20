"""Small incremental Server-Sent Events parser used by the CLI client."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from nico_agent.cli.errors import CliError


@dataclass(frozen=True, slots=True)
class SseEvent:
    id: str | None
    event: str
    data: str

    def json(self) -> dict[str, Any]:
        try:
            value = json.loads(self.data)
        except ValueError as exc:
            raise CliError(
                "INVALID_SSE_EVENT",
                "Nico API returned an SSE event with invalid JSON data",
            ) from exc
        if not isinstance(value, dict):
            raise CliError(
                "INVALID_SSE_EVENT",
                "Nico API returned an SSE event whose data is not an object",
            )
        return value


class SseParser:
    """Parse arbitrary text chunks without assuming network line boundaries."""

    def __init__(self) -> None:
        self._buffer = ""
        self._last_event_id: str | None = None
        self._event_name = "message"
        self._data: list[str] = []

    def feed(self, chunk: str) -> list[SseEvent]:
        self._buffer += chunk
        events: list[SseEvent] = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.endswith("\r"):
                line = line[:-1]
            event = self._line(line)
            if event is not None:
                events.append(event)
        return events

    def finish(self) -> list[SseEvent]:
        events: list[SseEvent] = []
        if self._buffer:
            event = self._line(self._buffer.removesuffix("\r"))
            self._buffer = ""
            if event is not None:
                events.append(event)
        event = self._dispatch()
        if event is not None:
            events.append(event)
        return events

    def _line(self, line: str) -> SseEvent | None:
        if not line:
            return self._dispatch()
        if line.startswith(":"):
            return None
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "data":
            self._data.append(value)
        elif field == "event":
            self._event_name = value or "message"
        elif field == "id" and "\x00" not in value:
            self._last_event_id = value
        return None

    def _dispatch(self) -> SseEvent | None:
        if not self._data:
            self._event_name = "message"
            return None
        event = SseEvent(
            id=self._last_event_id,
            event=self._event_name,
            data="\n".join(self._data),
        )
        self._data = []
        self._event_name = "message"
        return event
