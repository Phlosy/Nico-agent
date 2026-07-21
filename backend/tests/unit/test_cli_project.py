from __future__ import annotations

from io import StringIO

import pytest

from nico_agent.cli.errors import CliError
from nico_agent.cli.output import Output
from nico_agent.cli.project import ProjectCli


class FakeProjectClient:
    def __init__(self) -> None:
        self.created: dict | None = None
        self.member_state: dict | None = None
        self.cadence_value: dict | None = None
        self.guidance: dict | None = None
        self.withdrawal: dict | None = None

    def list_projects(self):
        return [
            {
                "id": "project-1",
                "name": "Research",
                "kind": "shared",
                "status": "active",
                "revision": 3,
            }
        ]

    def get_project(self, project_id):
        return {**self.list_projects()[0], "id": project_id}

    def list_agents(self):
        return [
            {
                "id": "agent-lead",
                "name": "lead",
                "display_name": "Lead",
                "status": "ready",
            },
            {
                "id": "agent-member",
                "name": "worker",
                "display_name": "Worker",
                "status": "ready",
            },
        ]

    def preflight_project(self, **_kwargs):
        return {"compatible": True, "issues": []}

    def create_collaboration_project(self, **kwargs):
        self.created = kwargs
        return {"project": {"id": "project-new", "name": kwargs["name"]}}

    def list_project_members(self, _project_id):
        return [
            {
                "id": "membership-lead",
                "agent_id": "agent-lead",
                "role": "lead",
                "status": "active",
                "revision": 1,
            },
            {
                "id": "membership-worker",
                "agent_id": "agent-member",
                "role": "member",
                "status": "paused",
                "revision": 2,
            },
        ]

    def list_project_sessions(self, _project_id):
        return [
            {
                "id": "session-lead",
                "project_member_id": "membership-lead",
                "current_conversation_id": "conversation-lead",
                "status": "active",
            },
            {
                "id": "session-worker",
                "project_member_id": "membership-worker",
                "current_conversation_id": "conversation-worker",
                "status": "paused",
            },
        ]

    def open_project_session(self, project_id, session_id):
        return {
            "id": "conversation-lead",
            "project_id": project_id,
            "project_session_id": session_id,
            "agent_id": "agent-lead",
            "agent_version_id": "version-lead",
            "title": "Research — Lead",
        }

    def get_conversation(self, conversation_id):
        return {
            "id": conversation_id,
            "project_id": "project-1",
            "agent_id": "agent-member",
            "agent_version_id": "version-worker",
            "title": "Research — Worker",
        }

    def project_timeline(self, _project_id, _session_id, **_kwargs):
        return {
            "entries": [
                {
                    "sequence": 1,
                    "kind": "task",
                    "event_type": "TaskCreated",
                    "run_id": "run-1",
                }
            ],
            "next_cursor": None,
            "has_more": False,
        }

    def set_project_member_state(self, project_id, agent_id, **kwargs):
        self.member_state = {"project_id": project_id, "agent_id": agent_id, **kwargs}
        return {"member": {"agent_id": agent_id, "status": kwargs["target"]}}

    def request_project_sync(self, project_id, **_kwargs):
        return {"id": "cycle-1", "project_id": project_id, "status": "pending"}

    def update_project_cadence(self, project_id, **kwargs):
        self.cadence_value = {"project_id": project_id, **kwargs}
        return {"id": project_id, "supervision_cadence_seconds": kwargs["cadence_seconds"]}

    def list_project_supervision_cycles(self, project_id):
        return [{"id": "cycle-1", "project_id": project_id, "status": "completed"}]

    def get_run(self, run_id):
        return {"id": run_id, "status": "running", "revision": 4}

    def create_run_intervention(self, project_id, session_id, run_id, **kwargs):
        self.guidance = {
            "project_id": project_id,
            "session_id": session_id,
            "run_id": run_id,
            **kwargs,
        }
        return {"id": "intervention-1", "status": "pending", "revision": 1}

    def list_run_interventions(self, _project_id, _session_id, _run_id):
        return [{"id": "intervention-1", "status": "pending", "revision": 2}]

    def withdraw_run_intervention(self, project_id, session_id, run_id, intervention_id, **kwargs):
        self.withdrawal = {
            "project_id": project_id,
            "session_id": session_id,
            "run_id": run_id,
            "intervention_id": intervention_id,
            **kwargs,
        }
        return {"id": intervention_id, "status": "withdrawn", "revision": 3}

    def escalate_project_change(self, project_id, session_id, **kwargs):
        return {"id": "turn-lead", "project_id": project_id, "session_id": session_id, **kwargs}

    def cancel_run(self, run_id, **kwargs):
        return {"id": run_id, "status": "cancelled", "revision": kwargs["expected_revision"] + 1}


def _coordinator(client: FakeProjectClient | None = None) -> ProjectCli:
    return ProjectCli(
        client or FakeProjectClient(),
        Output(json_mode=True, stdout=StringIO(), stderr=StringIO()),
    )


def test_project_creation_resolves_names_and_preflights() -> None:
    client = FakeProjectClient()
    result = _coordinator(client).create(
        name="Delivery",
        goal="Ship the release",
        lead="lead",
        members=["worker"],
        description=None,
        acceptance={"tests": "passing"},
        cadence_seconds=3600,
        idempotency_key="project-create",
    )

    assert result["project"]["id"] == "project-new"
    assert client.created is not None
    assert client.created["lead_agent_id"] == "agent-lead"
    assert client.created["member_agent_ids"] == ["agent-member"]


def test_workspace_defaults_to_lead_and_paused_member_is_read_only() -> None:
    coordinator = _coordinator()

    lead = coordinator.workspace("Research", agent_reference=None)
    worker = coordinator.workspace("project-1", agent_reference="worker")

    assert lead["session"]["id"] == "session-lead"
    assert lead["read_only"] is False
    assert worker["session"]["id"] == "session-worker"
    assert worker["conversation"]["id"] == "conversation-worker"
    assert worker["read_only"] is True


def test_timeline_and_member_state_use_names_without_copying_ids() -> None:
    client = FakeProjectClient()
    coordinator = _coordinator(client)

    timeline = coordinator.timeline(
        "Research", agent_reference="worker", after_sequence=0, limit=20
    )
    changed = coordinator.set_member_state(
        "Research",
        "worker",
        target="removed",
        reason="Work complete",
        expected_project_revision=None,
        expected_member_revision=None,
    )

    assert timeline["timeline"]["entries"][0]["kind"] == "task"
    assert changed["member"]["status"] == "removed"
    assert client.member_state is not None
    assert client.member_state["expected_project_revision"] == 3
    assert client.member_state["expected_member_revision"] == 2


def test_project_and_agent_resolution_fail_deterministically() -> None:
    coordinator = _coordinator()

    with pytest.raises(CliError) as missing_project:
        coordinator.resolve_project("missing")
    with pytest.raises(CliError) as missing_agent:
        coordinator.resolve_agent("missing")

    assert missing_project.value.code == "PROJECT_NOT_FOUND"
    assert missing_agent.value.code == "AGENT_NOT_FOUND"


def test_supervision_and_guidance_workflows_use_current_revisions() -> None:
    client = FakeProjectClient()
    coordinator = _coordinator(client)

    sync = coordinator.sync("Research")
    cadence = coordinator.cadence(
        "Research",
        cadence_seconds=7200,
        expected_project_revision=None,
        reason="less noise",
    )
    guidance = coordinator.guide(
        "Research",
        agent_reference="lead",
        content="check the deployment logs",
        run_reference=None,
        expected_run_revision=None,
    )

    assert sync["status"] == "pending"
    assert cadence["supervision_cadence_seconds"] == 7200
    assert client.cadence_value is not None
    assert client.cadence_value["expected_project_revision"] == 3
    assert guidance["status"] == "pending"
    assert client.guidance is not None
    assert client.guidance["expected_run_revision"] == 4


def test_withdraw_and_cancel_are_scoped_to_selected_session_run() -> None:
    client = FakeProjectClient()
    coordinator = _coordinator(client)

    withdrawn = coordinator.withdraw(
        "Research",
        agent_reference="lead",
        run_reference=None,
        intervention_id="intervention-1",
        expected_intervention_revision=None,
        reason="superseded",
    )
    cancelled = coordinator.cancel(
        "Research",
        agent_reference="lead",
        run_reference=None,
        expected_run_revision=None,
    )

    assert withdrawn["status"] == "withdrawn"
    assert client.withdrawal is not None
    assert client.withdrawal["expected_intervention_revision"] == 2
    assert cancelled["status"] == "cancelled"


def test_invalid_cadence_and_foreign_run_fail_before_mutation() -> None:
    coordinator = _coordinator()

    with pytest.raises(CliError) as invalid_cadence:
        coordinator.cadence(
            "Research",
            cadence_seconds=299,
            expected_project_revision=None,
            reason=None,
        )
    with pytest.raises(CliError) as foreign_run:
        coordinator.guide(
            "Research",
            agent_reference="lead",
            content="unsafe",
            run_reference="run-other",
            expected_run_revision=1,
        )

    assert invalid_cadence.value.code == "PROJECT_CADENCE_INVALID"
    assert foreign_run.value.code == "PROJECT_SESSION_RUN_NOT_FOUND"
