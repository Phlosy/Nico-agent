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


def test_json_formatter_redacts_provider_credentials_and_secret_shaped_text() -> None:
    record = logging.LogRecord(
        "nico.provider",
        logging.ERROR,
        __file__,
        42,
        "request failed authorization=provider-canary-secret",
        (),
        None,
    )
    record.credential_ref = "provider-canary-secret"
    record.provider = {
        "cookie": "provider-canary-secret",
        "endpoint_identity": "sha256:public",
    }

    rendered = JsonFormatter().format(record)
    payload = json.loads(rendered)

    assert "provider-canary-secret" not in rendered
    assert payload["credential_ref"] == "[REDACTED]"
    assert payload["provider"]["cookie"] == "[REDACTED]"
    assert payload["provider"]["endpoint_identity"] == "sha256:public"
