"""Small structured-logging layer based on the Python standard library."""

from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, TextIO

request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)

_STANDARD_RECORD_FIELDS = set(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
}
_SENSITIVE_LOG_KEY = re.compile(
    r"(?i)(?:api[_-]?key|authorization|cookie|credential|password|private[_-]?key|secret|token)"
)
_SENSITIVE_LOG_TEXT = re.compile(
    r"(?i)((?:api[_-]?key|authorization|cookie|credential|password|private[_-]?key|"
    r"secret|token)\s*[=:]\s*)[^\s,;]+"
)


class JsonFormatter(logging.Formatter):
    """Serialize each record as one JSON object for machine-readable logs."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": _redact_log_value(record.getMessage()),
        }
        request_id = request_id_context.get()
        if request_id:
            payload["request_id"] = request_id

        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_FIELDS and not key.startswith("_"):
                payload[key] = (
                    "[REDACTED]"
                    if _SENSITIVE_LOG_KEY.search(key)
                    else _json_safe(_redact_log_value(value))
                )

        if record.exc_info:
            payload["exception"] = _redact_log_value(self.formatException(record.exc_info))

        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return str(value)
    return value


def _redact_log_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]" if _SENSITIVE_LOG_KEY.search(str(key)) else _redact_log_value(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_redact_log_value(item) for item in value]
    if isinstance(value, str):
        return _SENSITIVE_LOG_TEXT.sub(r"\1[REDACTED]", value)
    return value


def configure_logging(level: str = "INFO", stream: TextIO | None = None) -> None:
    """Configure root and framework loggers with a single JSON handler."""

    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    for logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.propagate = True
