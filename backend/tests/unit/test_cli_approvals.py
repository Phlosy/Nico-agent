from __future__ import annotations

from io import StringIO

from nico_agent.cli.approvals import ApprovalCoordinator
from nico_agent.cli.output import Output


class FakeClient:
    def __init__(self) -> None:
        self.command = None

    def decide_tool_approval(self, approval_id, **command):
        self.command = {"approval_id": approval_id, **command}
        return {"id": approval_id, "status": command["decision"]}


def coordinator(client, *, interactive=False, answer=""):
    return ApprovalCoordinator(
        client,
        Output(no_color=True, stdout=StringIO(), stderr=StringIO()),
        interactive=interactive,
        prompt=lambda _message: answer,
    )


def approval(*, status="requested"):
    return {"id": "approval-1", "revision": 3, "status": status}


def test_automatic_approval_uses_run_scope() -> None:
    client = FakeClient()
    assert coordinator(client).decide(approval(), automatically_approve=True) is True
    assert client.command["decision"] == "approve"
    assert client.command["allowed_scope"] == "run"


def test_interactive_rejection_is_persisted() -> None:
    client = FakeClient()
    assert coordinator(client, interactive=True, answer="3").decide(approval()) is False
    assert client.command["decision"] == "reject"
    assert client.command["allowed_scope"] is None


def test_noninteractive_pending_approval_is_left_durable() -> None:
    client = FakeClient()
    assert coordinator(client).decide(approval()) is False
    assert client.command is None


def test_already_decided_approval_is_idempotent() -> None:
    client = FakeClient()
    assert coordinator(client).decide(approval(status="approved")) is True
    assert client.command is None
