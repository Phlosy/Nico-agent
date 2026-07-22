"""Resumable four-area guided setup orchestration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from nico_agent.cli.approvals import ApprovalCoordinator
from nico_agent.cli.capabilities import CapabilityCoordinator, CapabilityInput
from nico_agent.cli.client import NicoApiClient
from nico_agent.cli.errors import CliError
from nico_agent.cli.execution import RunWatcher
from nico_agent.cli.output import Output

_PROOF_PROMPT = (
    "Run Nico's bounded online verification for the configured Search and Fetch capabilities."
)


@dataclass(slots=True)
class GuidedSetupInput:
    skip_web: bool = False
    enable_web: bool = False
    automatically_approve_tools: bool = False
    reconfigure: str | None = None


class GuidedSetupCoordinator:
    def __init__(
        self,
        client: NicoApiClient,
        output: Output,
        *,
        interactive: bool,
        prompt: Callable[..., str],
        confirm: Callable[..., bool],
        model_setup: Callable[[], dict[str, Any]],
        web_setup: Callable[[], dict[str, Any]],
        capabilities: CapabilityCoordinator,
    ) -> None:
        self.client = client
        self.output = output
        self.interactive = interactive
        self.prompt = prompt
        self.confirm = confirm
        self.model_setup = model_setup
        self.web_setup = web_setup
        self.capabilities = capabilities

    def run(
        self,
        values: GuidedSetupInput,
        capability_values: CapabilityInput,
    ) -> dict[str, Any]:
        if values.skip_web and values.enable_web:
            raise CliError(
                "SETUP_WEB_CHOICE_CONFLICT",
                "--skip-web and --enable-web cannot be used together",
                exit_code=2,
            )
        readiness = self.client.setup_readiness()
        self._checklist(readiness)
        maintenance = self._maintenance_choice(readiness, values.reconfigure)
        if values.skip_web and maintenance == "web":
            raise CliError(
                "SETUP_WEB_CHOICE_CONFLICT",
                "--skip-web cannot be combined with Web reconfiguration",
                exit_code=2,
            )
        if self._area(readiness, "model").get("state") != "ready" or maintenance == "model":
            self.model_setup()
            readiness = self.client.setup_readiness()
            if self._area(readiness, "model").get("state") != "ready":
                raise CliError(
                    "SETUP_MODEL_INCOMPLETE",
                    "model setup did not produce a verified active route",
                    exit_code=4,
                )
            self._checklist(readiness)

        web_area = self._area(readiness, "web")
        resume_skipped = web_area.get("state") == "skipped" and values.enable_web
        if (
            web_area.get("state") not in {"ready", "skipped"}
            or resume_skipped
            or maintenance == "web"
        ):
            enable_web = values.enable_web
            if maintenance == "web":
                enable_web = True
            elif not values.enable_web and not values.skip_web:
                if not self.interactive:
                    raise CliError(
                        "SETUP_WEB_CHOICE_REQUIRED",
                        "pass --enable-web or --skip-web in non-interactive setup",
                        exit_code=2,
                    )
                enable_web = self.confirm(
                    "Enable Web Search with the recommended SearXNG Provider?",
                    default=True,
                )
            if values.skip_web or not enable_web:
                readiness = self._skip_web(readiness)
            else:
                self.web_setup()
                readiness = self.client.setup_readiness()
                if self._area(readiness, "web").get("state") != "ready":
                    raise CliError(
                        "SETUP_WEB_INCOMPLETE",
                        "Web setup did not produce a verified active Provider",
                        exit_code=4,
                    )
            self._checklist(readiness)
        elif web_area.get("state") == "skipped" and self.interactive and not values.skip_web:
            if self.confirm("Web setup was skipped. Resume it now?", default=False):
                self.web_setup()
                readiness = self.client.setup_readiness()
                if self._area(readiness, "web").get("state") != "ready":
                    raise CliError(
                        "SETUP_WEB_INCOMPLETE",
                        "Web setup did not produce a verified active Provider",
                        exit_code=4,
                    )
                self._checklist(readiness)

        if (
            self._area(readiness, "capabilities").get("state") != "ready"
            or maintenance == "capabilities"
        ):
            target = readiness.get("target")
            if not isinstance(target, dict) or not target.get("agent_id"):
                raise CliError(
                    "SETUP_AGENT_REQUIRED",
                    "a verified model Agent is required before capability setup",
                    exit_code=3,
                )
            capability_values.agent_id = str(target["agent_id"])
            capability_values.expected_agent_revision = int(target["agent_revision"])
            self.capabilities.publish(capability_values)
            readiness = self.client.setup_readiness()
            if self._area(readiness, "capabilities").get("state") != "ready":
                raise CliError(
                    "SETUP_CAPABILITIES_INCOMPLETE",
                    "capability publication did not become current",
                    exit_code=4,
                )
            self._checklist(readiness)

        web_state = self._area(readiness, "web").get("state")
        proof_state = self._area(readiness, "verification").get("state")
        if (
            web_state == "ready"
            and proof_state != "blocked"
            and (proof_state != "ready" or maintenance == "verification")
        ):
            readiness = self._run_proof(
                readiness,
                automatically_approve_tools=values.automatically_approve_tools,
            )
            self._checklist(readiness)

        return self._summary(readiness)

    def _maintenance_choice(
        self,
        readiness: dict[str, Any],
        requested: str | None,
    ) -> str | None:
        choices = ("model", "web", "capabilities", "verification")
        if requested is not None:
            normalized = requested.strip().lower()
            if normalized not in choices:
                raise CliError(
                    "SETUP_MAINTENANCE_INVALID",
                    f"unknown setup area '{requested}'",
                    exit_code=2,
                )
            return normalized
        if readiness.get("overall") != "full" or not self.interactive:
            return None
        self.output.table(
            [
                {"number": 1, "area": "model", "action": "change model route"},
                {"number": 2, "area": "web", "action": "change Web Provider"},
                {"number": 3, "area": "capabilities", "action": "publish capabilities"},
                {"number": 4, "area": "verification", "action": "run online proof again"},
                {"number": 5, "area": "exit", "action": "keep the current setup"},
            ],
            title="Setup Maintenance",
            columns=["number", "area", "action"],
        )
        answer = self.prompt("Maintenance area number or key", default="5").strip().lower()
        if answer in {"", "5", "exit", "none"}:
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(choices):
            return choices[int(answer) - 1]
        if answer in choices:
            return answer
        raise CliError(
            "SETUP_MAINTENANCE_INVALID",
            f"unknown setup area '{answer}'",
            exit_code=2,
        )

    def _skip_web(self, readiness: dict[str, Any]) -> dict[str, Any]:
        updated = self.client.update_setup_intent(
            {
                "expected_tenant_revision": readiness["tenant_revision"],
                "web_intent": "skipped",
            }
        )
        del updated
        return self.client.setup_readiness()

    def _run_proof(
        self,
        readiness: dict[str, Any],
        *,
        automatically_approve_tools: bool,
    ) -> dict[str, Any]:
        target = readiness.get("target")
        if not isinstance(target, dict):
            raise CliError("SETUP_PROOF_TARGET_MISSING", "online proof has no selected Agent")
        conversation = self.client.create_conversation(
            project_id=None,
            agent_id=str(target["agent_id"]),
            mode="personal",
            agent_version_id=str(target["agent_version_id"]),
            title="Nico setup online verification",
            idempotency_key=f"setup-proof-conversation-{uuid4().hex}",
        )
        turn = self.client.create_conversation_turn(
            str(conversation["id"]),
            _PROOF_PROMPT,
            idempotency_key=f"setup-proof-turn-{uuid4().hex}",
            max_steps=3,
            token_budget=1,
            timeout_seconds=120,
            budgets={
                "tool_allow": ["web.search@1.0.0", "web.fetch@1.1.0"],
                "setup_proof": True,
            },
        )
        after_sequence = 0
        final_run: dict[str, Any] | None = None
        approvals = ApprovalCoordinator(
            self.client,
            self.output,
            interactive=self.interactive,
            prompt=lambda message: self.prompt(message),
        )
        for _ in range(8):
            watched = RunWatcher(self.client, self.output).watch(
                str(turn["run_id"]),
                after_sequence=after_sequence,
            )
            final_run = watched["run"]
            approval = watched.get("approval_required")
            if approval is None:
                break
            if not approvals.decide(
                approval,
                automatically_approve=automatically_approve_tools,
            ):
                raise CliError(
                    "SETUP_PROOF_APPROVAL_REQUIRED",
                    "online proof is waiting for a Tool approval",
                    exit_code=4,
                )
            events = self.client.list_run_events(str(turn["run_id"]))
            after_sequence = max(
                (int(event.get("sequence") or 0) for event in events),
                default=after_sequence,
            )
        if final_run is None:
            final_run = self.client.get_run(str(turn["run_id"]))
        current = self.client.setup_readiness()
        validated = self.client.validate_setup_proof(
            expected_tenant_revision=int(current["tenant_revision"]),
            run_id=str(turn["run_id"]),
        )
        if validated.get("state") != "succeeded":
            raise CliError(
                "SETUP_PROOF_FAILED",
                f"online verification failed: {validated.get('failure_class') or 'unknown'}",
                exit_code=4,
            )
        return self.client.setup_readiness()

    def _checklist(self, readiness: dict[str, Any]) -> None:
        if self.output.json_mode:
            return
        self.output.table(
            [
                {
                    "step": f"{index}/4 {area.get('key')}",
                    "state": area.get("state"),
                    "summary": area.get("summary"),
                    "next": area.get("next_action"),
                }
                for index, area in enumerate(readiness.get("areas") or [], start=1)
            ],
            title="Nico Setup",
            columns=["step", "state", "summary", "next"],
        )

    @staticmethod
    def _area(readiness: dict[str, Any], key: str) -> dict[str, Any]:
        return next(
            (item for item in readiness.get("areas") or [] if item.get("key") == key),
            {},
        )

    @staticmethod
    def _summary(readiness: dict[str, Any]) -> dict[str, Any]:
        target = readiness.get("target") or {}
        return {
            "status": readiness.get("overall"),
            "readiness": readiness,
            "model": target.get("model_name"),
            "web_provider": readiness.get("web_provider"),
            "agent": target.get("agent_name"),
            "agent_id": target.get("agent_id"),
            "agent_version": target.get("agent_version"),
            "capability_profile": readiness.get("selected_profile"),
            "verified_at": readiness.get("verified_at"),
            "chat_command": (
                f"nico chat --agent {target.get('agent_id')}" if target.get("agent_id") else None
            ),
        }
