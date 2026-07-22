"""Reusable terminal selector for AgentVersion capability publication."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nico_agent.cli.client import NicoApiClient
from nico_agent.cli.errors import CliError
from nico_agent.cli.output import Output

_PROFILES = {"minimal", "web_research", "developer", "custom"}
_RISKS = {"medium", "high"}


@dataclass(slots=True)
class CapabilityInput:
    agent_id: str | None = None
    expected_agent_revision: int | None = None
    starter_agent_name: str | None = None
    starter_agent_display_name: str | None = None
    source_agent_version_id: str | None = None
    profile: str | None = None
    tool_refs: list[str] = field(default_factory=list)
    skill_version_ids: list[str] = field(default_factory=list)
    select_all: bool = False
    clear_all: bool = False
    accepted_risks: list[str] = field(default_factory=list)
    confirmed: bool = False


class CapabilityCoordinator:
    def __init__(
        self,
        client: NicoApiClient,
        output: Output,
        *,
        interactive: bool,
        prompt: Callable[..., str],
        confirm: Callable[..., bool],
    ) -> None:
        self.client = client
        self.output = output
        self.interactive = interactive
        self.prompt = prompt
        self.confirm = confirm

    def publish(self, values: CapabilityInput) -> dict[str, Any]:
        if values.select_all and values.clear_all:
            raise CliError(
                "CAPABILITY_SELECTION_CONFLICT",
                "--all and --clear cannot be used together",
                exit_code=2,
            )
        catalog = self.client.capability_catalog(agent_id=values.agent_id)
        if catalog.get("schema_version") != 1:
            raise CliError(
                "CAPABILITY_CATALOG_UNSUPPORTED",
                "this CLI does not support the server capability catalog schema",
                exit_code=3,
            )
        profile = self._profile(catalog, values.profile)
        tool_refs, skill_ids = self._selection(catalog, profile, values)
        target = self._target(values)
        preview_command = {
            "expected_tenant_revision": catalog["tenant_revision"],
            "target": target,
            "selection": {
                "profile": profile,
                "tool_refs": tool_refs if profile == "custom" else [],
                "skill_version_ids": skill_ids if profile == "custom" else [],
            },
        }
        preview = self.client.preview_capabilities(preview_command)
        self._show_preview(preview)
        accepted_risks = self._accept_risks(preview, values.accepted_risks)
        confirmed = values.confirmed
        if not confirmed and self.interactive:
            confirmed = self.confirm("Publish this Agent capability version?", default=False)
        if not confirmed:
            raise CliError(
                "CAPABILITY_ACTIVATION_CANCELLED",
                "Agent capability publication was not confirmed",
                exit_code=2,
            )
        activation = self.client.activate_capabilities(
            {
                **preview_command,
                "preview_hash": preview["preview_hash"],
                "accepted_risks": accepted_risks,
            }
        )
        return {
            "status": "ready",
            "activation": activation,
            "new_conversation_required": True,
            "chat_command": f"nico chat --agent {activation['agent_id']}",
        }

    def _profile(self, catalog: dict[str, Any], requested: str | None) -> str:
        if requested is not None:
            normalized = requested.strip().lower().replace("-", "_")
            if normalized not in _PROFILES:
                raise CliError(
                    "CAPABILITY_PROFILE_INVALID",
                    f"unknown capability profile '{requested}'",
                    exit_code=2,
                )
            return normalized
        self._require_interactive("capability profile")
        profiles = catalog.get("profiles") or []
        self.output.table(
            [
                {
                    "number": index,
                    "profile": item.get("key"),
                    "description": item.get("description"),
                    "recommended": "yes" if item.get("recommended") else "",
                }
                for index, item in enumerate(profiles, start=1)
            ],
            title="Capability Profiles",
            columns=["number", "profile", "description", "recommended"],
        )
        recommended = next((item for item in profiles if item.get("recommended")), profiles[0])
        answer = self.prompt(
            "Profile number or key",
            default=str(recommended.get("key")),
        ).strip()
        if answer.isdigit() and 1 <= int(answer) <= len(profiles):
            return str(profiles[int(answer) - 1]["key"])
        return self._profile(catalog, answer)

    def _selection(
        self,
        catalog: dict[str, Any],
        profile: str,
        values: CapabilityInput,
    ) -> tuple[list[str], list[str]]:
        if profile != "custom":
            has_custom_selection = bool(
                values.tool_refs
                or values.skill_version_ids
                or values.select_all
                or values.clear_all
            )
            if has_custom_selection:
                raise CliError(
                    "CAPABILITY_SELECTION_CONFLICT",
                    "individual selections require the custom profile",
                    exit_code=2,
                )
            return [], []
        tools = catalog.get("tools") or []
        skills = catalog.get("skills") or []
        usable_tools = {str(item["reference"]) for item in tools if item.get("usable")}
        usable_skills = {str(item["skill_version_id"]) for item in skills if item.get("usable")}
        if values.select_all:
            return sorted(usable_tools), sorted(usable_skills)
        if values.clear_all:
            return [], []
        if values.tool_refs or values.skill_version_ids:
            self._validate_explicit(values.tool_refs, usable_tools, "Tool")
            self._validate_explicit(values.skill_version_ids, usable_skills, "Skill")
            return sorted(set(values.tool_refs)), sorted(set(values.skill_version_ids))
        self._require_interactive("custom capability selection")
        rows: list[dict[str, Any]] = []
        choices: list[tuple[str, str, bool]] = []
        for item in tools:
            choices.append(("tool", str(item["reference"]), bool(item.get("usable"))))
            rows.append(
                {
                    "number": len(choices),
                    "kind": "Tool",
                    "name": item.get("reference"),
                    "description": item.get("description"),
                    "risk_trust": item.get("risk"),
                    "state": "usable" if item.get("usable") else item.get("reason_code"),
                    "reason": "" if item.get("usable") else item.get("reason"),
                }
            )
        for item in skills:
            choices.append(("skill", str(item["skill_version_id"]), bool(item.get("usable"))))
            rows.append(
                {
                    "number": len(choices),
                    "kind": "Skill",
                    "name": f"{item.get('name')} v{item.get('version')}",
                    "description": item.get("description"),
                    "risk_trust": item.get("trust"),
                    "state": "usable" if item.get("usable") else item.get("reason_code"),
                    "reason": "" if item.get("usable") else item.get("reason"),
                }
            )
        self.output.table(
            rows,
            title="Agent Capabilities",
            columns=[
                "number",
                "kind",
                "name",
                "description",
                "risk_trust",
                "state",
                "reason",
            ],
        )
        if not skills and not self.output.json_mode:
            self.output.out.print("[dim]Skills: no eligible published Skills.[/dim]")
        answer = (
            self.prompt(
                "Capability numbers (comma-separated), 'all', or 'none'",
                default="none",
            )
            .strip()
            .lower()
        )
        if answer == "all":
            return sorted(usable_tools), sorted(usable_skills)
        if answer in {"none", "clear", ""}:
            return [], []
        selected_tools: list[str] = []
        selected_skills: list[str] = []
        for raw in answer.split(","):
            value = raw.strip()
            if not value.isdigit() or not 1 <= int(value) <= len(choices):
                raise CliError(
                    "CAPABILITY_SELECTION_INVALID",
                    f"'{value}' is not a valid capability number",
                    exit_code=2,
                )
            kind, identifier, usable = choices[int(value) - 1]
            if not usable:
                raise CliError(
                    "CAPABILITY_UNAVAILABLE",
                    f"capability {value} is unavailable; resolve its listed dependency first",
                    exit_code=2,
                )
            (selected_tools if kind == "tool" else selected_skills).append(identifier)
        return sorted(set(selected_tools)), sorted(set(selected_skills))

    def _target(self, values: CapabilityInput) -> dict[str, Any]:
        if values.agent_id is not None:
            agent = self.client.get_agent(values.agent_id)
            revision = values.expected_agent_revision or agent.get("revision")
            if not isinstance(revision, int):
                raise CliError(
                    "CAPABILITY_AGENT_REVISION_REQUIRED",
                    "the current Agent revision is required",
                    exit_code=2,
                )
            return {
                "agent_id": values.agent_id,
                "expected_agent_revision": revision,
            }
        if not all(
            (
                values.starter_agent_name,
                values.starter_agent_display_name,
                values.source_agent_version_id,
            )
        ):
            raise CliError(
                "CAPABILITY_STARTER_REQUIRED",
                "Starter publication requires name, display name, and source AgentVersion",
                exit_code=2,
            )
        return {
            "starter_agent_name": values.starter_agent_name,
            "starter_agent_display_name": values.starter_agent_display_name,
            "source_agent_version_id": values.source_agent_version_id,
        }

    def _accept_risks(
        self,
        preview: dict[str, Any],
        supplied: list[str],
    ) -> list[str]:
        risks = [str(item) for item in preview.get("risks") or []]
        invalid = sorted(set(supplied) - _RISKS)
        if invalid:
            raise CliError(
                "CAPABILITY_RISK_INVALID",
                f"unsupported risk confirmation: {', '.join(invalid)}",
                exit_code=2,
            )
        accepted = set(supplied)
        missing = sorted(set(risks) - accepted)
        if missing and self.interactive:
            approved = self.confirm(
                f"Grant Tools with elevated risk ({', '.join(missing)})?",
                default=False,
            )
            if approved:
                accepted.update(missing)
                missing = []
        if missing:
            raise CliError(
                "CAPABILITY_RISK_CONFIRMATION_REQUIRED",
                f"confirm elevated risks with --accept-risk: {', '.join(missing)}",
                exit_code=2,
            )
        return sorted(accepted)

    def _show_preview(self, preview: dict[str, Any]) -> None:
        if self.output.json_mode:
            return
        diff = preview.get("diff") or {}
        self.output.table(
            [
                {
                    "agent": preview.get("target_agent_name"),
                    "new_version": preview.get("proposed_agent_version"),
                    "tools_added": len(diff.get("tools_added") or []),
                    "tools_removed": len(diff.get("tools_removed") or []),
                    "skills_added": len(diff.get("skills_added") or []),
                    "skills_removed": len(diff.get("skills_removed") or []),
                    "model_changed": diff.get("model_route_changed", False),
                }
            ],
            title="Agent Capability Preview",
            columns=[
                "agent",
                "new_version",
                "tools_added",
                "tools_removed",
                "skills_added",
                "skills_removed",
                "model_changed",
            ],
        )

    @staticmethod
    def _validate_explicit(values: list[str], usable: set[str], kind: str) -> None:
        unavailable = sorted(set(values) - usable)
        if unavailable:
            raise CliError(
                "CAPABILITY_UNAVAILABLE",
                f"unavailable {kind} selections: {', '.join(unavailable)}",
                exit_code=2,
            )

    def _require_interactive(self, field: str) -> None:
        if not self.interactive:
            raise CliError(
                "CAPABILITY_INPUT_REQUIRED",
                f"non-interactive capability publication requires {field} flags",
                exit_code=2,
            )
