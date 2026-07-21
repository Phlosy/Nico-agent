"""High-level CLI workflows for managed Project workspaces."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

from nico_agent.cli.client import NicoApiClient
from nico_agent.cli.errors import CliError
from nico_agent.cli.output import Output


class ProjectCli:
    def __init__(
        self,
        client: NicoApiClient,
        output: Output,
        *,
        interactive: bool = False,
        prompt: Callable[[str], str] | None = None,
    ) -> None:
        self.client = client
        self.output = output
        self.interactive = interactive
        self.prompt = prompt

    def resolve_project(self, reference: str) -> dict[str, Any]:
        projects = self.client.list_projects()
        matches = [
            item
            for item in projects
            if str(item.get("id")) == reference or item.get("name") == reference
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise CliError(
                "PROJECT_NAME_AMBIGUOUS",
                self._candidate_message("Project", reference, matches),
                exit_code=2,
            )
        raise CliError(
            "PROJECT_NOT_FOUND",
            f"no Project matches '{reference}'; run 'nico project list'",
            exit_code=2,
        )

    def resolve_agent(
        self,
        reference: str | None,
        *,
        ready_only: bool = True,
        label: str = "Agent",
    ) -> dict[str, Any]:
        agents = self.client.list_agents()
        if ready_only:
            agents = [item for item in agents if item.get("status") == "ready"]
        if reference is not None:
            matches = [
                item
                for item in agents
                if str(item.get("id")) == reference or item.get("name") == reference
            ]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise CliError(
                    "AGENT_NAME_AMBIGUOUS",
                    self._candidate_message("Agent", reference, matches),
                    exit_code=2,
                )
            raise CliError(
                "AGENT_NOT_FOUND",
                f"no {'ready ' if ready_only else ''}Agent matches '{reference}'",
                exit_code=2,
            )
        if not agents:
            raise CliError("READY_AGENT_REQUIRED", "no ready Agent is available", exit_code=2)
        if len(agents) == 1:
            return agents[0]
        if not self.interactive or self.prompt is None:
            raise CliError(
                "PROJECT_AGENT_REQUIRED",
                f"multiple ready Agents are available; pass --{label.lower().replace(' ', '-')}",
                exit_code=2,
            )
        choices = [{"selection": index, **item} for index, item in enumerate(agents, start=1)]
        self.output.table(
            choices,
            title=f"Choose {label}",
            columns=["selection", "name", "display_name", "status", "id"],
        )
        answer = self.prompt(f"{label} number, exact name, or ID: ").strip()
        if not answer:
            raise CliError("PROJECT_SELECTION_CANCELLED", "selection was cancelled", exit_code=2)
        if answer.isdecimal() and 1 <= int(answer) <= len(agents):
            return agents[int(answer) - 1]
        return self.resolve_agent(answer, ready_only=ready_only, label=label)

    def create(
        self,
        *,
        name: str,
        goal: str,
        lead: str | None,
        members: list[str],
        description: str | None,
        acceptance: dict[str, Any],
        cadence_seconds: int | None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if not name.strip() or not goal.strip():
            raise CliError(
                "PROJECT_INPUT_REQUIRED", "Project name and goal cannot be blank", exit_code=2
            )
        lead_agent = self.resolve_agent(lead, label="Lead")
        member_agents = [self.resolve_agent(item, label="Member") for item in members]
        member_ids = [str(item["id"]) for item in member_agents]
        if str(lead_agent["id"]) in member_ids:
            raise CliError(
                "PROJECT_MEMBER_DUPLICATE",
                "the Lead must not be repeated in --member",
                exit_code=2,
            )
        if len(set(member_ids)) != len(member_ids):
            raise CliError(
                "PROJECT_MEMBER_DUPLICATE", "--member values must be unique", exit_code=2
            )
        preflight = self.client.preflight_project(
            lead_agent_id=str(lead_agent["id"]), member_agent_ids=member_ids
        )
        if not preflight.get("compatible"):
            issues = "; ".join(str(item) for item in preflight.get("issues") or [])
            raise CliError(
                "PROJECT_LEAD_INCOMPATIBLE",
                issues or "the selected Lead cannot coordinate this Project",
                exit_code=2,
            )
        return self.client.create_collaboration_project(
            name=name.strip(),
            description=description.strip() if description else None,
            goal=goal.strip(),
            acceptance=acceptance,
            lead_agent_id=str(lead_agent["id"]),
            member_agent_ids=member_ids,
            supervision_cadence_seconds=cadence_seconds,
            idempotency_key=idempotency_key or str(uuid4()),
        )

    def status(self, project_reference: str) -> dict[str, Any]:
        project = self.resolve_project(project_reference)
        project_id = str(project["id"])
        members = self.client.list_project_members(project_id)
        sessions = self.client.list_project_sessions(project_id)
        lead = next(
            (
                item
                for item in members
                if item.get("role") == "lead" and item.get("status") == "active"
            ),
            None,
        )
        return {
            "project": self.client.get_project(project_id),
            "lead_agent_id": lead.get("agent_id") if lead else None,
            "members": members,
            "sessions": sessions,
        }

    def add_member(
        self,
        project_reference: str,
        agent_reference: str,
        *,
        expected_project_revision: int | None,
    ) -> dict[str, Any]:
        project = self.resolve_project(project_reference)
        agent = self.resolve_agent(agent_reference, label="Member")
        revision = expected_project_revision or int(project["revision"])
        return self.client.add_project_member(
            str(project["id"]),
            agent_id=str(agent["id"]),
            expected_project_revision=revision,
            idempotency_key=str(uuid4()),
        )

    def set_member_state(
        self,
        project_reference: str,
        agent_reference: str,
        *,
        target: str,
        reason: str | None,
        expected_project_revision: int | None,
        expected_member_revision: int | None,
    ) -> dict[str, Any]:
        project = self.resolve_project(project_reference)
        member, _ = self._resolve_project_member(project, agent_reference)
        return self.client.set_project_member_state(
            str(project["id"]),
            str(member["agent_id"]),
            target=target,
            expected_project_revision=(expected_project_revision or int(project["revision"])),
            expected_member_revision=(expected_member_revision or int(member["revision"])),
            reason=reason,
            idempotency_key=str(uuid4()),
        )

    def replace_lead(
        self,
        project_reference: str,
        agent_reference: str,
        *,
        expected_project_revision: int | None,
    ) -> dict[str, Any]:
        project = self.resolve_project(project_reference)
        agent = self.resolve_agent(agent_reference, label="Lead")
        return self.client.replace_project_lead(
            str(project["id"]),
            new_lead_agent_id=str(agent["id"]),
            expected_project_revision=(expected_project_revision or int(project["revision"])),
            idempotency_key=str(uuid4()),
        )

    def workspace(
        self,
        project_reference: str,
        *,
        agent_reference: str | None,
    ) -> dict[str, Any]:
        project = self.resolve_project(project_reference)
        project_id = str(project["id"])
        members = self.client.list_project_members(project_id)
        sessions = self.client.list_project_sessions(project_id)
        membership, agents = self._resolve_project_member(project, agent_reference, members=members)
        project_session = next(
            (
                item
                for item in sessions
                if str(item.get("project_member_id")) == str(membership["id"])
            ),
            None,
        )
        if project_session is None:
            raise CliError("PROJECT_SESSION_NOT_FOUND", "the selected member has no stable Session")
        read_only = (
            project.get("status") != "active"
            or membership.get("status") != "active"
            or project_session.get("status") != "active"
        )
        conversation = (
            self.client.open_project_session(project_id, str(project_session["id"]))
            if not read_only
            else self._current_conversation(project_session)
        )
        return {
            "project": project,
            "member": membership,
            "agent": agents.get(str(membership["agent_id"]), {}),
            "session": project_session,
            "conversation": conversation,
            "read_only": read_only,
        }

    def timeline(
        self,
        project_reference: str,
        *,
        agent_reference: str | None,
        after_sequence: int,
        limit: int,
    ) -> dict[str, Any]:
        workspace = self.workspace(project_reference, agent_reference=agent_reference)
        page = self.client.project_timeline(
            str(workspace["project"]["id"]),
            str(workspace["session"]["id"]),
            after_sequence=after_sequence,
            limit=limit,
        )
        return {**workspace, "timeline": page}

    def _resolve_project_member(
        self,
        project: dict[str, Any],
        agent_reference: str | None,
        *,
        members: list[dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        values = (
            members if members is not None else self.client.list_project_members(str(project["id"]))
        )
        agents = {str(item["id"]): item for item in self.client.list_agents()}
        if agent_reference is None:
            membership = next(
                (
                    item
                    for item in values
                    if item.get("role") == "lead" and item.get("status") == "active"
                ),
                None,
            )
            if membership is None:
                raise CliError("PROJECT_LEAD_REQUIRED", "the Project has no active Lead")
            return membership, agents
        matches = [
            item
            for item in values
            if str(item.get("agent_id")) == agent_reference
            or agents.get(str(item.get("agent_id")), {}).get("name") == agent_reference
        ]
        if len(matches) != 1:
            code = "PROJECT_MEMBER_NOT_FOUND" if not matches else "AGENT_NAME_AMBIGUOUS"
            raise CliError(
                code,
                f"Project member reference '{agent_reference}' did not resolve uniquely",
                exit_code=2,
            )
        return matches[0], agents

    def _current_conversation(self, project_session: dict[str, Any]) -> dict[str, Any]:
        conversation_id = project_session.get("current_conversation_id")
        if not conversation_id:
            raise CliError(
                "PROJECT_SESSION_EMPTY", "the read-only Session has no Conversation history"
            )
        return self.client.get_conversation(str(conversation_id))

    @staticmethod
    def _candidate_message(kind: str, reference: str, candidates: list[dict[str, Any]]) -> str:
        values = ", ".join(f"{item.get('name')} ({item.get('id')})" for item in candidates)
        return f"{kind} reference '{reference}' is ambiguous: {values}"
