"""Synchronous REST client for the Nico control plane."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import httpx

from nico_agent.cli.config import ResolvedProfile
from nico_agent.cli.errors import CliError
from nico_agent.cli.sse import SseParser


class NicoApiClient:
    def __init__(
        self,
        profile: ResolvedProfile,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.profile = profile
        self._client = httpx.Client(
            base_url=f"{profile.base_url}/",
            timeout=profile.timeout_seconds,
            verify=profile.verify_tls,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def __enter__(self) -> NicoApiClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        require_tenant: bool = True,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        headers = self._headers(require_tenant=require_tenant)
        headers.update(extra_headers or {})
        try:
            response = self._client.request(
                method,
                path.lstrip("/"),
                headers=headers,
                params=params,
                json=json_body,
            )
        except httpx.TimeoutException as exc:
            raise CliError(
                "API_TIMEOUT", "the Nico API request timed out", status_code=None
            ) from exc
        except httpx.HTTPError as exc:
            raise CliError(
                "API_UNREACHABLE",
                f"cannot reach Nico API at {self.profile.base_url}",
            ) from exc
        request_id = response.headers.get("X-Request-ID")
        if response.is_error:
            raise _response_error(response, request_id)
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise CliError(
                "INVALID_API_RESPONSE",
                "Nico API returned a non-JSON response",
                request_id=request_id,
                status_code=response.status_code,
            ) from exc

    def liveness(self) -> dict[str, Any]:
        return self.request("GET", "/api/v1/health/live", require_tenant=False)

    def readiness(self) -> dict[str, Any]:
        return self.request("GET", "/api/v1/health/ready", require_tenant=False)

    def list_projects(self, *, include_system: bool = False) -> list[dict[str, Any]]:
        return self.request(
            "GET",
            "/api/v1/projects",
            params={"include_system": include_system},
        )

    def get_project(self, project_id: str) -> dict[str, Any]:
        return self.request("GET", f"/api/v1/projects/{project_id}")

    def preflight_project(
        self, *, lead_agent_id: str, member_agent_ids: list[str]
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            "/api/v1/projects/collaboration/preflight",
            json_body={
                "lead_agent_id": lead_agent_id,
                "member_agent_ids": member_agent_ids,
            },
        )

    def create_collaboration_project(
        self,
        *,
        name: str,
        description: str | None,
        goal: str,
        acceptance: dict[str, Any],
        lead_agent_id: str,
        member_agent_ids: list[str],
        supervision_cadence_seconds: int | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            "/api/v1/projects/collaboration",
            json_body={
                "name": name,
                "description": description,
                "goal": goal,
                "acceptance": acceptance,
                "lead_agent_id": lead_agent_id,
                "member_agent_ids": member_agent_ids,
                "supervision_cadence_seconds": supervision_cadence_seconds,
            },
            extra_headers={"Idempotency-Key": idempotency_key},
        )

    def list_project_members(self, project_id: str) -> list[dict[str, Any]]:
        return self.request("GET", f"/api/v1/projects/{project_id}/members")

    def list_project_sessions(self, project_id: str) -> list[dict[str, Any]]:
        return self.request("GET", f"/api/v1/projects/{project_id}/sessions")

    def open_project_session(self, project_id: str, session_id: str) -> dict[str, Any]:
        return self.request("POST", f"/api/v1/projects/{project_id}/sessions/{session_id}/open")

    def project_timeline(
        self,
        project_id: str,
        session_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        return self.request(
            "GET",
            f"/api/v1/projects/{project_id}/sessions/{session_id}/timeline",
            params={"after_sequence": after_sequence, "limit": limit},
        )

    def add_project_member(
        self,
        project_id: str,
        *,
        agent_id: str,
        expected_project_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/api/v1/projects/{project_id}/members",
            json_body={
                "agent_id": agent_id,
                "expected_project_revision": expected_project_revision,
            },
            extra_headers={"Idempotency-Key": idempotency_key},
        )

    def set_project_member_state(
        self,
        project_id: str,
        agent_id: str,
        *,
        target: str,
        expected_project_revision: int,
        expected_member_revision: int,
        reason: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/api/v1/projects/{project_id}/members/{agent_id}/state",
            json_body={
                "target": target,
                "expected_project_revision": expected_project_revision,
                "expected_member_revision": expected_member_revision,
                "reason": reason,
            },
            extra_headers={"Idempotency-Key": idempotency_key},
        )

    def replace_project_lead(
        self,
        project_id: str,
        *,
        new_lead_agent_id: str,
        expected_project_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/api/v1/projects/{project_id}/lead",
            json_body={
                "new_lead_agent_id": new_lead_agent_id,
                "expected_project_revision": expected_project_revision,
            },
            extra_headers={"Idempotency-Key": idempotency_key},
        )

    def request_project_sync(self, project_id: str, *, idempotency_key: str) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/api/v1/projects/{project_id}/supervision/sync",
            json_body={},
            extra_headers={"Idempotency-Key": idempotency_key},
        )

    def update_project_cadence(
        self,
        project_id: str,
        *,
        cadence_seconds: int | None,
        expected_project_revision: int,
        reason: str | None,
    ) -> dict[str, Any]:
        return self.request(
            "PATCH",
            f"/api/v1/projects/{project_id}/supervision/cadence",
            json_body={
                "cadence_seconds": cadence_seconds,
                "expected_project_revision": expected_project_revision,
                "reason": reason,
            },
        )

    def list_project_supervision_cycles(self, project_id: str) -> list[dict[str, Any]]:
        return self.request("GET", f"/api/v1/projects/{project_id}/supervision/cycles")

    def create_run_intervention(
        self,
        project_id: str,
        session_id: str,
        run_id: str,
        *,
        content: str,
        expected_run_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/api/v1/projects/{project_id}/sessions/{session_id}/runs/{run_id}/interventions",
            json_body={
                "content": content,
                "expected_run_revision": expected_run_revision,
            },
            extra_headers={"Idempotency-Key": idempotency_key},
        )

    def list_run_interventions(
        self, project_id: str, session_id: str, run_id: str
    ) -> list[dict[str, Any]]:
        return self.request(
            "GET",
            f"/api/v1/projects/{project_id}/sessions/{session_id}/runs/{run_id}/interventions",
        )

    def withdraw_run_intervention(
        self,
        project_id: str,
        session_id: str,
        run_id: str,
        intervention_id: str,
        *,
        expected_intervention_revision: int,
        reason: str | None,
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/api/v1/projects/{project_id}/sessions/{session_id}/runs/{run_id}/"
            f"interventions/{intervention_id}/withdraw",
            json_body={
                "expected_intervention_revision": expected_intervention_revision,
                "reason": reason,
            },
        )

    def escalate_project_change(
        self,
        project_id: str,
        session_id: str,
        *,
        content: str,
        max_steps: int,
        token_budget: int | None,
        timeout_seconds: int | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/api/v1/projects/{project_id}/sessions/{session_id}/project-changes",
            json_body={
                "content": content,
                "max_steps": max_steps,
                "token_budget": token_budget,
                "timeout_seconds": timeout_seconds,
            },
            extra_headers={"Idempotency-Key": idempotency_key},
        )

    def list_agents(self) -> list[dict[str, Any]]:
        return self.request("GET", "/api/v1/agents")

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        return self.request("GET", f"/api/v1/agents/{agent_id}")

    def list_agent_versions(self, agent_id: str) -> list[dict[str, Any]]:
        return self.request("GET", f"/api/v1/agents/{agent_id}/versions")

    def provider_catalog(self) -> dict[str, Any]:
        return self.request("GET", "/api/v1/provider-catalog", require_tenant=False)

    def provider_setup_readiness(self) -> dict[str, Any]:
        return self.request("GET", "/api/v1/provider-setup-readiness")

    def list_provider_connections(self) -> list[dict[str, Any]]:
        return self.request("GET", "/api/v1/provider-connections")

    def create_provider_probe(
        self,
        *,
        kind: str,
        candidate: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            "/api/v1/provider-probes",
            json_body={
                "kind": kind,
                "candidate": candidate,
                "idempotency_key": idempotency_key,
            },
        )

    def get_provider_probe(self, probe_id: str) -> dict[str, Any]:
        return self.request("GET", f"/api/v1/provider-probes/{probe_id}")

    def cancel_provider_probe(self, probe_id: str) -> dict[str, Any]:
        return self.request("POST", f"/api/v1/provider-probes/{probe_id}/cancel")

    def preview_provider_activation(
        self,
        *,
        probe_id: str,
        candidate_hash: str,
        target: dict[str, Any],
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            "/api/v1/provider-activation/preview",
            json_body={
                "probe_id": probe_id,
                "candidate_hash": candidate_hash,
                "target": target,
            },
        )

    def activate_provider(
        self,
        *,
        probe_id: str,
        candidate_hash: str,
        target: dict[str, Any],
        preview_hash: str,
        maintenance_attempt_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "probe_id": probe_id,
            "candidate_hash": candidate_hash,
            "target": target,
            "preview_hash": preview_hash,
        }
        if maintenance_attempt_id is not None:
            body["maintenance_attempt_id"] = maintenance_attempt_id
        return self.request(
            "POST",
            "/api/v1/provider-activation",
            json_body=body,
        )

    def web_catalog(self) -> dict[str, Any]:
        return self.request("GET", "/api/v1/web/catalog", require_tenant=False)

    def web_setup_readiness(self) -> dict[str, Any]:
        return self.request("GET", "/api/v1/web/setup-readiness")

    def web_status(self) -> dict[str, Any]:
        return self.request("GET", "/api/v1/web/status")

    def create_web_probe(
        self,
        *,
        candidate: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            "/api/v1/web/provider-probes",
            json_body={"candidate": candidate, "idempotency_key": idempotency_key},
        )

    def get_web_probe(self, probe_id: str) -> dict[str, Any]:
        return self.request("GET", f"/api/v1/web/provider-probes/{probe_id}")

    def test_web_configuration(self, *, idempotency_key: str) -> dict[str, Any]:
        return self.request(
            "POST",
            "/api/v1/web/test",
            json_body={"idempotency_key": idempotency_key},
        )

    def preview_web_activation(
        self,
        *,
        probe_id: str,
        candidate_hash: str,
        target: dict[str, Any],
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            "/api/v1/web/activation/preview",
            json_body={
                "probe_id": probe_id,
                "candidate_hash": candidate_hash,
                "target": target,
            },
        )

    def activate_web(
        self,
        *,
        probe_id: str,
        candidate_hash: str,
        target: dict[str, Any],
        preview_hash: str,
        maintenance_attempt_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "probe_id": probe_id,
            "candidate_hash": candidate_hash,
            "target": target,
            "preview_hash": preview_hash,
        }
        if maintenance_attempt_id is not None:
            body["maintenance_attempt_id"] = maintenance_attempt_id
        return self.request("POST", "/api/v1/web/activation", json_body=body)

    def preview_web_disable(self, *, target: dict[str, Any]) -> dict[str, Any]:
        return self.request(
            "POST",
            "/api/v1/web/disable/preview",
            json_body={"target": target},
        )

    def disable_web(self, *, target: dict[str, Any], preview_hash: str) -> dict[str, Any]:
        return self.request(
            "POST",
            "/api/v1/web/disable",
            json_body={"target": target, "preview_hash": preview_hash},
        )

    def get_task(self, task_id: str) -> dict[str, Any]:
        return self.request("GET", f"/api/v1/tasks/{task_id}")

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self.request("GET", f"/api/v1/runs/{run_id}")

    def cancel_run(self, run_id: str, *, expected_revision: int) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/api/v1/runs/{run_id}/cancel",
            json_body={"expected_revision": expected_revision},
        )

    def get_runtime(self, run_id: str) -> dict[str, Any]:
        return self.request("GET", f"/api/v1/runs/{run_id}/runtime")

    def list_run_events(self, run_id: str) -> list[dict[str, Any]]:
        return self.request("GET", f"/api/v1/runs/{run_id}/events")

    def list_run_steps(self, run_id: str) -> list[dict[str, Any]]:
        return self.request("GET", f"/api/v1/runs/{run_id}/steps")

    def list_run_tool_calls(self, run_id: str) -> list[dict[str, Any]]:
        return self.request("GET", f"/api/v1/runs/{run_id}/tool-calls")

    def list_run_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        return self.request("GET", f"/api/v1/runs/{run_id}/artifacts")

    def list_run_plans(self, run_id: str) -> list[dict[str, Any]]:
        return self.request("GET", f"/api/v1/runs/{run_id}/plans")

    def list_plan_steps(self, run_id: str, plan_id: str) -> list[dict[str, Any]]:
        return self.request("GET", f"/api/v1/runs/{run_id}/plans/{plan_id}/steps")

    def list_run_children(self, run_id: str) -> list[dict[str, Any]]:
        return self.request("GET", f"/api/v1/runs/{run_id}/children")

    def list_run_messages(self, run_id: str) -> list[dict[str, Any]]:
        return self.request("GET", f"/api/v1/runs/{run_id}/messages")

    def list_run_contexts(self, run_id: str) -> list[dict[str, Any]]:
        return self.request("GET", f"/api/v1/runs/{run_id}/contexts")

    def list_audit(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return self.request("GET", "/api/v1/audit", params={"limit": limit})

    def list_tool_approvals(
        self,
        *,
        run_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if run_id is not None:
            params["run_id"] = run_id
        if status is not None:
            params["status"] = status
        return self.request("GET", "/api/v1/tool-approval-requests", params=params)

    def get_tool_approval(self, approval_id: str) -> dict[str, Any]:
        return self.request("GET", f"/api/v1/tool-approval-requests/{approval_id}")

    def decide_tool_approval(
        self,
        approval_id: str,
        *,
        expected_revision: int,
        decision: str,
        allowed_scope: str | None,
        reason: str | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "expected_revision": expected_revision,
            "decision": decision,
            "allowed_scope": allowed_scope,
        }
        if reason is not None:
            body["reason"] = reason
        return self.request(
            "POST",
            f"/api/v1/tool-approval-requests/{approval_id}/decision",
            json_body=body,
            extra_headers={"Idempotency-Key": idempotency_key},
        )

    def create_conversation(
        self,
        *,
        project_id: str | None,
        agent_id: str,
        mode: str = "project",
        agent_version_id: str | None = None,
        title: str = "New conversation",
        idempotency_key: str,
    ) -> dict[str, Any]:
        body = {"mode": mode, "agent_id": agent_id, "title": title}
        if project_id is not None:
            body["project_id"] = project_id
        if agent_version_id is not None:
            body["agent_version_id"] = agent_version_id
        return self.request(
            "POST",
            "/api/v1/conversations",
            json_body=body,
            extra_headers={"Idempotency-Key": idempotency_key},
        )

    def list_conversations(
        self,
        *,
        project_id: str | None = None,
        agent_id: str | None = None,
        status: str | None = None,
        mode: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if project_id is not None:
            params["project_id"] = project_id
        if agent_id is not None:
            params["agent_id"] = agent_id
        if status is not None:
            params["status"] = status
        if mode is not None:
            params["mode"] = mode
        return self.request("GET", "/api/v1/conversations", params=params)

    def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        return self.request("GET", f"/api/v1/conversations/{conversation_id}")

    def update_conversation(
        self,
        conversation_id: str,
        *,
        expected_revision: int,
        title: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"expected_revision": expected_revision}
        if title is not None:
            body["title"] = title
        if status is not None:
            body["status"] = status
        return self.request(
            "PATCH",
            f"/api/v1/conversations/{conversation_id}",
            json_body=body,
        )

    def list_conversation_turns(
        self,
        conversation_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self.request(
            "GET",
            f"/api/v1/conversations/{conversation_id}/turns",
            params={"after_sequence": after_sequence, "limit": limit},
        )

    def create_conversation_turn(
        self,
        conversation_id: str,
        message: str,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/api/v1/conversations/{conversation_id}/turns",
            json_body={"user_input": message},
            extra_headers={"Idempotency-Key": idempotency_key},
        )

    def get_conversation_turn(self, turn_id: str) -> dict[str, Any]:
        return self.request("GET", f"/api/v1/conversation-turns/{turn_id}")

    def cancel_conversation_turn(
        self, turn_id: str, *, expected_run_revision: int
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/api/v1/conversation-turns/{turn_id}/cancel",
            json_body={"expected_revision": expected_run_revision},
        )

    def retry_conversation_turn(
        self,
        turn_id: str,
        *,
        expected_run_id: str,
        expected_run_revision: int,
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/api/v1/conversation-turns/{turn_id}/retry",
            json_body={
                "expected_run_id": expected_run_id,
                "expected_run_revision": expected_run_revision,
            },
        )

    def compact_conversation(self, conversation_id: str, *, idempotency_key: str) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/api/v1/conversations/{conversation_id}/compact",
            json_body={},
            extra_headers={"Idempotency-Key": idempotency_key},
        )

    def upload_conversation_attachment(
        self,
        conversation_id: str,
        *,
        name: str,
        content_type: str,
        idempotency_key: str,
        data: bytes,
    ) -> dict[str, Any]:
        headers = self._headers(require_tenant=True)
        try:
            response = self._client.post(
                f"api/v1/conversations/{conversation_id}/attachments",
                headers=headers,
                params={
                    "name": name,
                    "content_type": content_type,
                    "idempotency_key": idempotency_key,
                },
                content=data,
            )
        except httpx.TimeoutException as exc:
            raise CliError("API_TIMEOUT", "the attachment upload timed out") from exc
        except httpx.HTTPError as exc:
            raise CliError("API_UNREACHABLE", "cannot reach the Nico API") from exc
        if response.is_error:
            raise _response_error(response, response.headers.get("X-Request-ID"))
        return response.json()

    def list_conversation_attachments(self, conversation_id: str) -> list[dict[str, Any]]:
        return self.request("GET", f"/api/v1/conversations/{conversation_id}/attachments")

    def download_artifact(self, run_id: str, artifact_id: str) -> tuple[bytes, str | None]:
        try:
            response = self._client.get(
                f"api/v1/runs/{run_id}/artifacts/{artifact_id}/content",
                headers=self._headers(require_tenant=True),
            )
        except httpx.TimeoutException as exc:
            raise CliError("API_TIMEOUT", "the Artifact download timed out") from exc
        except httpx.HTTPError as exc:
            raise CliError("API_UNREACHABLE", "cannot reach the Nico API") from exc
        if response.is_error:
            raise _response_error(response, response.headers.get("X-Request-ID"))
        return response.content, response.headers.get("content-type")

    def stream_run_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        reconnect_attempts: int = 3,
    ) -> Iterator[dict[str, Any]]:
        cursor = max(0, after_sequence)
        failures = 0
        while True:
            headers = self._headers(require_tenant=True)
            headers.update(
                {
                    "Accept": "text/event-stream",
                    "Last-Event-ID": str(cursor),
                }
            )
            try:
                with self._client.stream(
                    "GET",
                    f"api/v1/runs/{run_id}/events/stream",
                    headers=headers,
                ) as response:
                    request_id = response.headers.get("X-Request-ID")
                    if response.is_error:
                        response.read()
                        raise _response_error(response, request_id)
                    content_type = response.headers.get("content-type", "")
                    if "text/event-stream" not in content_type:
                        raise CliError(
                            "INVALID_API_RESPONSE",
                            "Nico API did not return an SSE event stream",
                            request_id=request_id,
                            status_code=response.status_code,
                        )
                    parser = SseParser()
                    for chunk in response.iter_text():
                        for event in parser.feed(chunk):
                            payload = event.json()
                            sequence = payload.get("sequence")
                            if not isinstance(sequence, int):
                                raise CliError(
                                    "INVALID_SSE_EVENT",
                                    "Nico SSE event is missing an integer sequence",
                                )
                            if sequence <= cursor:
                                continue
                            cursor = sequence
                            yield payload
                    for event in parser.finish():
                        payload = event.json()
                        sequence = payload.get("sequence")
                        if isinstance(sequence, int) and sequence > cursor:
                            cursor = sequence
                            yield payload
                    return
            except CliError:
                raise
            except httpx.TimeoutException as exc:
                failures += 1
                if failures > reconnect_attempts:
                    raise CliError(
                        "SSE_TIMEOUT",
                        "the Nico event stream timed out after reconnect attempts",
                    ) from exc
            except httpx.HTTPError as exc:
                failures += 1
                if failures > reconnect_attempts:
                    raise CliError(
                        "SSE_DISCONNECTED",
                        "the Nico event stream disconnected after reconnect attempts",
                    ) from exc

    def _headers(self, *, require_tenant: bool) -> dict[str, str]:
        headers = {"Accept": "application/json", "X-Request-ID": str(uuid4())}
        if require_tenant:
            if self.profile.tenant_id is None:
                raise CliError(
                    "TENANT_CONTEXT_REQUIRED",
                    "this command requires tenant_id; configure a profile or set NICO_TENANT_ID",
                    exit_code=2,
                )
            headers["X-Tenant-ID"] = str(self.profile.tenant_id)
            headers["X-Actor-ID"] = self.profile.actor_id
        if self.profile.api_token:
            headers["Authorization"] = f"Bearer {self.profile.api_token}"
        return headers


def _response_error(response: httpx.Response, request_id: str | None) -> CliError:
    code = f"HTTP_{response.status_code}"
    message = f"Nico API returned HTTP {response.status_code}"
    details: Any | None = None
    try:
        body = response.json()
        if isinstance(body, dict):
            if isinstance(body.get("code"), str):
                code = body["code"]
            if isinstance(body.get("message"), str):
                message = body["message"]
            elif isinstance(body.get("detail"), str):
                message = body["detail"]
            elif isinstance(body.get("detail"), list):
                message = "request validation failed"
                details = body["detail"]
    except ValueError:
        pass
    return CliError(
        code,
        message,
        request_id=request_id,
        status_code=response.status_code,
        details=details,
    )
