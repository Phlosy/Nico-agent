import json
import logging

from nico_agent.logging import JsonFormatter, request_id_context


def test_json_formatter_includes_context_and_structured_fields() -> None:
    token = request_id_context.set("request-42")
    try:
        record = logging.LogRecord(
            name="nico.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=12,
            msg="dependency checked",
            args=(),
            exc_info=None,
        )
        record.component = "redis"

        payload = json.loads(JsonFormatter().format(record))
    finally:
        request_id_context.reset(token)

    assert payload["level"] == "INFO"
    assert payload["message"] == "dependency checked"
    assert payload["request_id"] == "request-42"
    assert payload["component"] == "redis"
    assert payload["timestamp"].endswith("Z")
