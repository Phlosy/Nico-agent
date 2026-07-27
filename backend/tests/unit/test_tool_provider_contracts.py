from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from nico_agent.tool_providers import (
    ExternalToolProviderCreate,
    ExternalToolProviderStatus,
    ProviderCapabilitiesResponse,
    ProviderFeatureSet,
    ProviderToolCallRequest,
    ProviderToolCallResponse,
    ProviderToolContract,
    ProviderToolError,
    ProviderToolIdentity,
    ProviderTrace,
    PublicRunToolBindingPolicy,
    RunToolBindingBudget,
    RunToolBindingCreate,
    RunToolBindingPolicy,
    RunToolBindingScope,
    RunToolBindingSnapshot,
    ToolBindingApprovalMode,
    ToolProviderError,
    ToolProviderErrorCode,
    binding_digest,
    provider_error,
    provider_status_can_transition,
    schema_digest,
    sign_hmac_request,
    validate_provider_endpoint,
    verify_hmac_request,
    verify_signed_body_identity,
)

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
DIGEST_A = f"sha256:{'a' * 64}"
DIGEST_B = f"sha256:{'b' * 64}"
DIGEST_C = f"sha256:{'c' * 64}"
SECRET = "provider-test-secret-that-is-at-least-32-bytes"


def _tool_contract() -> ProviderToolContract:
    return ProviderToolContract(
        name="example.exec",
        version="1.0.0",
        input_schema_digest=DIGEST_A,
        output_schema_digest=DIGEST_B,
    )


def _request(*, attempt: int = 1) -> ProviderToolCallRequest:
    return ProviderToolCallRequest(
        provider_id="provider_01",
        binding_digest=DIGEST_C,
        request_id="req_01",
        tool_call_id="call_01",
        idempotency_key="runtime-action-7",
        tenant_id=uuid4(),
        project_id=uuid4(),
        run_id=uuid4(),
        task_id=uuid4(),
        agent_id=uuid4(),
        agent_version_id=uuid4(),
        tool=_tool_contract(),
        arguments={"command": "echo hello"},
        deadline=NOW + timedelta(minutes=2),
        attempt=attempt,
        trace=ProviderTrace(trace_id="trace_01", parent_event_id="event_01"),
    )


def _snapshot(**overrides: object) -> RunToolBindingSnapshot:
    values: dict[str, object] = {
        "binding_id": uuid4(),
        "provider_id": uuid4(),
        "provider_name": "stub.provider",
        "endpoint_identity": DIGEST_C,
        "credential_ref": "env:NICO_TOOL_SECRET_STUB_PROVIDER",
        "scope": RunToolBindingScope(
            tenant_id=uuid4(),
            project_id=uuid4(),
            run_id=uuid4(),
            task_id=uuid4(),
            agent_id=uuid4(),
            agent_version_id=uuid4(),
        ),
        "tool": _tool_contract(),
        "capability_digest": DIGEST_A,
        "policy": RunToolBindingPolicy(
            approval=ToolBindingApprovalMode.INHERIT,
            budget=RunToolBindingBudget(
                max_calls=3,
                max_total_duration_ms=30_000,
                max_single_call_duration_ms=10_000,
                max_retries=1,
            ),
            provider_request_timeout_ms=8_000,
        ),
        "provider_expires_at": NOW + timedelta(hours=1),
        "binding_expires_at": NOW + timedelta(minutes=30),
        "frozen_at": NOW,
    }
    values.update(overrides)
    return RunToolBindingSnapshot(**values)


def test_schema_digest_is_canonical_and_rejects_non_finite_json() -> None:
    first = {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    }
    second = {
        "required": ["command"],
        "properties": {"command": {"type": "string"}},
        "type": "object",
    }

    assert schema_digest(first) == schema_digest(second)
    assert schema_digest(first).startswith("sha256:")
    assert len(schema_digest(first)) == 71

    with pytest.raises(ValueError):
        schema_digest({"number": float("nan")})


def test_provider_create_is_secret_reference_only_and_endpoint_is_normalized() -> None:
    provider = ExternalToolProviderCreate(
        name="shell.provider",
        endpoint_url="https://PROVIDER.EXAMPLE.TEST:443/base/",
        credential_ref="env:NICO_TOOL_SECRET_SHELL_PROVIDER",
        metadata={"deployment": "test"},
    )

    assert provider.endpoint_ref == "https://provider.example.test/base"
    assert provider.endpoint_url == provider.endpoint_ref
    assert "NICO_TOOL_SECRET_SHELL_PROVIDER" not in repr(provider)

    with pytest.raises(ValidationError):
        ExternalToolProviderCreate(
            name="shell.provider",
            endpoint_url="https://provider.example.test",
            credential_ref="plaintext-api-key",
        )
    with pytest.raises(ValidationError, match=r"env:NICO_TOOL_SECRET"):
        ExternalToolProviderCreate(
            name="shell.provider",
            endpoint_url="https://provider.example.test",
            credential_ref="secret:providers/shell",
        )
    with pytest.raises(ValidationError):
        ExternalToolProviderCreate(
            name="shell.provider",
            endpoint_url="https://provider.example.test",
            credential_ref="env:NICO_TOOL_SECRET_SHELL_PROVIDER",
            metadata={"api_key": "must-not-be-here"},
        )


def test_endpoint_validation_is_https_by_default_and_loopback_is_explicit() -> None:
    with pytest.raises(ValueError):
        validate_provider_endpoint("http://localhost:8765")
    assert (
        validate_provider_endpoint("http://localhost:8765/", allow_http_loopback=True)
        == "http://localhost:8765"
    )
    assert (
        validate_provider_endpoint("http://127.0.0.1:8765/", allow_http_loopback=True)
        == "http://127.0.0.1:8765"
    )
    assert (
        validate_provider_endpoint("http://[::1]:8765/", allow_http_loopback=True)
        == "http://[::1]:8765"
    )
    assert (
        validate_provider_endpoint("http://provider.internal:8765/", allow_http=True)
        == "http://provider.internal:8765"
    )
    with pytest.raises(ValueError):
        validate_provider_endpoint("https://user:password@provider.example.test")
    with pytest.raises(ValueError):
        validate_provider_endpoint("https://provider.example.test?redirect=metadata")


def test_provider_capability_contract_is_exact_unique_and_v1_safe() -> None:
    capabilities = ProviderCapabilitiesResponse(
        provider_id="provider_01",
        supported_tools=(_tool_contract(),),
        features=ProviderFeatureSet(cancellation=True),
    )

    assert capabilities.capability_digest.startswith("sha256:")

    with pytest.raises(ValidationError):
        ProviderCapabilitiesResponse(
            provider_id="provider_01",
            supported_tools=(_tool_contract(), _tool_contract()),
            features=ProviderFeatureSet(),
        )
    with pytest.raises(ValidationError):
        ProviderFeatureSet(idempotency=False)
    with pytest.raises(ValidationError):
        ProviderFeatureSet(streaming=True)


def test_provider_lifecycle_is_explicit_and_terminal_states_stay_terminal() -> None:
    assert provider_status_can_transition(
        ExternalToolProviderStatus.REGISTERED,
        ExternalToolProviderStatus.VERIFIED,
    )
    assert provider_status_can_transition(
        ExternalToolProviderStatus.DISABLED,
        ExternalToolProviderStatus.ACTIVE,
    )
    assert not provider_status_can_transition(
        ExternalToolProviderStatus.REVOKED,
        ExternalToolProviderStatus.ACTIVE,
    )


def test_request_identity_and_digest_are_stable_across_retries() -> None:
    first = _request(attempt=1)
    second = first.model_copy(update={"attempt": 2})

    assert first.request_digest == second.request_digest
    assert first.tool.reference == "example.exec@1.0.0"

    invalid = _request().model_dump()
    invalid["deadline"] = datetime(2026, 7, 27)
    with pytest.raises(ValidationError):
        ProviderToolCallRequest.model_validate(invalid)


def test_execute_response_enforces_status_shape_and_timestamp_order() -> None:
    response = ProviderToolCallResponse(
        provider_id="provider_01",
        binding_digest=DIGEST_C,
        request_id="req_01",
        tool_call_id="call_01",
        tool=ProviderToolIdentity(name="example.exec", version="1.0.0"),
        status="succeeded",
        result={"stdout": "hello\n", "exit_code": 0},
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=1),
        provider_execution_id="exec_01",
    )

    assert response.error is None

    with pytest.raises(ValidationError):
        ProviderToolCallResponse(
            provider_id="provider_01",
            binding_digest=DIGEST_C,
            request_id="req_01",
            tool_call_id="call_01",
            tool=ProviderToolIdentity(name="example.exec", version="1.0.0"),
            status="failed",
            result={"unsafe": "must not be accepted"},
            error=ProviderToolError(
                code="PROVIDER_COMMAND_FAILED",
                category="execution",
                retryable=False,
                message="command failed",
            ),
            started_at=NOW,
            finished_at=NOW + timedelta(seconds=1),
            provider_execution_id="exec_01",
        )
    invalid = response.model_dump()
    invalid["started_at"] = NOW + timedelta(seconds=2)
    invalid["finished_at"] = NOW
    with pytest.raises(ValidationError):
        ProviderToolCallResponse.model_validate(invalid)


def test_provider_error_message_is_bounded_safe_and_redacted() -> None:
    error = ProviderToolError(
        code="PROVIDER_COMMAND_FAILED",
        category="execution",
        retryable=False,
        message="command failed; Authorization: bearer-secret",
    )

    assert "bearer-secret" not in error.message
    assert "[REDACTED]" in error.message
    with pytest.raises(ValidationError):
        ProviderToolError(
            code="PROVIDER_COMMAND_FAILED",
            category="execution",
            retryable=False,
            message="line one\nline two",
        )


def test_binding_snapshot_computes_and_verifies_immutable_digest() -> None:
    snapshot = _snapshot()

    assert snapshot.binding_digest is not None
    assert snapshot.binding_digest == binding_digest(snapshot)
    assert "NICO_TOOL_SECRET_STUB_PROVIDER" not in repr(snapshot)

    payload = snapshot.model_dump(mode="json")
    payload["policy"]["budget"]["max_calls"] = 4
    with pytest.raises(ValidationError, match="binding_digest"):
        RunToolBindingSnapshot.model_validate(payload)
    secret_reference = snapshot.model_dump(mode="json")
    secret_reference["credential_ref"] = "secret:providers/stub"
    secret_reference["binding_digest"] = None
    with pytest.raises(ValidationError, match=r"env:NICO_TOOL_SECRET"):
        RunToolBindingSnapshot.model_validate(secret_reference)
    with pytest.raises(ValidationError):
        snapshot.policy.budget.max_calls = 4


def test_binding_budget_rejects_unenforceable_timeout_combinations() -> None:
    with pytest.raises(ValidationError):
        RunToolBindingBudget(
            max_calls=1,
            max_total_duration_ms=5_000,
            max_single_call_duration_ms=10_000,
        )
    with pytest.raises(ValidationError):
        RunToolBindingPolicy(
            budget=RunToolBindingBudget(
                max_calls=1,
                max_total_duration_ms=10_000,
                max_single_call_duration_ms=10_000,
            ),
            provider_request_timeout_ms=20_000,
        )


def test_run_create_publishes_public_policy_and_converts_to_frozen_shape() -> None:
    binding = RunToolBindingCreate(
        provider_id=uuid4(),
        tool=ProviderToolIdentity(name="example.exec", version="1.0.0"),
        policy={
            "timeout_seconds": 10,
            "max_calls": 3,
            "retry": {"max_attempts": 2},
            "approval_mode": "never",
        },
    )

    assert isinstance(binding.policy, PublicRunToolBindingPolicy)
    assert (
        RunToolBindingCreate.model_json_schema()["properties"]["policy"]["$ref"]
        == "#/$defs/PublicRunToolBindingPolicy"
    )
    frozen = binding.policy.to_frozen_policy()
    assert frozen.provider_request_timeout_ms == 10_000
    assert frozen.budget.max_calls == 3
    assert frozen.budget.max_total_duration_ms == 30_000
    assert frozen.budget.max_single_call_duration_ms == 10_000
    assert frozen.budget.max_retries == 1
    assert frozen.approval == ToolBindingApprovalMode.NEVER


def test_public_policy_caps_derived_total_duration_at_one_day() -> None:
    policy = PublicRunToolBindingPolicy(
        timeout_seconds=300,
        max_calls=100_000,
    )

    assert policy.max_single_call_duration == 300
    assert policy.max_total_duration == 86_400
    assert policy.to_frozen_policy().budget.max_total_duration_ms == 86_400_000

    explicit_boundary = PublicRunToolBindingPolicy(
        timeout_seconds=300,
        max_calls=100_000,
        max_total_duration=86_400,
    )
    assert explicit_boundary.max_total_duration == 86_400

    with pytest.raises(ValidationError):
        PublicRunToolBindingPolicy(
            timeout_seconds=300,
            max_calls=100_000,
            max_total_duration=86_401,
        )


def test_hmac_sign_and_verify_is_canonical_fresh_and_replay_safe() -> None:
    body = b'{"command":"echo hello"}'
    signed = sign_hmac_request(
        secret=SECRET,
        method="POST",
        path_with_query="/v1/tool-calls?mode=sync",
        body=body,
        provider_id="provider_01",
        binding_digest=DIGEST_C,
        request_id="req_01",
        timestamp=int(NOW.timestamp()),
        nonce="0123456789abcdef0123456789abcdef",
    )
    consumed: set[str] = set()

    def consume_nonce(nonce: str, _timestamp: int) -> bool:
        if nonce in consumed:
            return False
        consumed.add(nonce)
        return True

    verified = verify_hmac_request(
        headers=signed.as_http_headers(),
        secret=SECRET,
        method="POST",
        path_with_query="/v1/tool-calls?mode=sync",
        body=body,
        now=NOW,
        consume_nonce=consume_nonce,
    )

    assert verified.provider_id == "provider_01"
    assert "Nico-HMAC-SHA256" not in repr(signed)
    verify_signed_body_identity(
        verified,
        {
            "provider_id": "provider_01",
            "binding_digest": DIGEST_C,
            "request_id": "req_01",
        },
    )

    with pytest.raises(ToolProviderError) as mismatch:
        verify_signed_body_identity(
            verified,
            {
                "provider_id": "provider_other",
                "binding_digest": DIGEST_C,
                "request_id": "req_01",
            },
        )
    assert mismatch.value.cause == "SIGNED_IDENTITY_MISMATCH"

    with pytest.raises(ToolProviderError) as replay:
        verify_hmac_request(
            headers=signed,
            secret=SECRET,
            method="POST",
            path_with_query="/v1/tool-calls?mode=sync",
            body=body,
            now=NOW,
            consume_nonce=consume_nonce,
        )
    assert replay.value.code == ToolProviderErrorCode.AUTH_ERROR
    assert replay.value.cause == "NONCE_REPLAY"


@pytest.mark.parametrize(
    ("body", "now", "cause"),
    [
        (b'{"command":"different"}', NOW, "CONTENT_DIGEST_MISMATCH"),
        (b'{"command":"echo hello"}', NOW + timedelta(seconds=61), "SIGNATURE_STALE"),
    ],
)
def test_hmac_verification_fails_closed_without_consuming_nonce(
    body: bytes,
    now: datetime,
    cause: str,
) -> None:
    signed = sign_hmac_request(
        secret=SECRET,
        method="POST",
        path_with_query="/v1/tool-calls",
        body=b'{"command":"echo hello"}',
        provider_id="provider_01",
        binding_digest=DIGEST_C,
        request_id="req_01",
        timestamp=int(NOW.timestamp()),
        nonce="fedcba9876543210fedcba9876543210",
    )
    calls = 0

    def consume_nonce(_nonce: str, _timestamp: int) -> bool:
        nonlocal calls
        calls += 1
        return True

    with pytest.raises(ToolProviderError) as captured:
        verify_hmac_request(
            headers=signed,
            secret=SECRET,
            method="POST",
            path_with_query="/v1/tool-calls",
            body=body,
            now=now,
            consume_nonce=consume_nonce,
        )

    assert captured.value.cause == cause
    assert calls == 0


def test_all_public_error_codes_have_complete_non_sensitive_context() -> None:
    provider_id = uuid4()
    for code in ToolProviderErrorCode:
        error = provider_error(
            code,
            "provider failed\nwithout leaking transport details",
            run_id=uuid4(),
            tool_call_id=uuid4(),
            provider_id=provider_id,
            attempt=1,
            timestamp=NOW,
            cause="UPSTREAM_FAILURE",
        )

        assert error.code == code.value
        assert error.details["source"]
        assert error.details["category"]
        assert error.details["retryable"] is error.retryable
        assert UUID(error.details["provider_id"]) == provider_id
        assert "\n" not in error.message
