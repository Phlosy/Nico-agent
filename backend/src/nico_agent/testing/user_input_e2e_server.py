"""Small HTTP fixture used by the PTY UserInput restart proof."""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

TENANT_ID = "11111111-1111-4111-8111-111111111111"
CONVERSATION_ID = "22222222-2222-4222-8222-222222222222"
AGENT_ID = "33333333-3333-4333-8333-333333333333"
VERSION_ID = "44444444-4444-4444-8444-444444444444"
TURN_ID = "55555555-5555-4555-8555-555555555555"
RUN_ID = "66666666-6666-4666-8666-666666666666"
REQUEST_ID = "77777777-7777-4777-8777-777777777777"


class FixtureState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.answered = threading.Event()
        self.answer_count = 0
        self.turn_create_count = 0
        self.final_read_count = 0

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "answered": self.answered.is_set(),
                "answer_count": self.answer_count,
                "turn_create_count": self.turn_create_count,
                "final_read_count": self.final_read_count,
            }


STATE = FixtureState()


class Handler(BaseHTTPRequestHandler):
    server_version = "NicoUserInputFixture/1"

    def log_message(self, _format: str, *_args: Any) -> None:
        return None

    def _json(self, value: Any, *, status: int = 200) -> None:
        body = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-ID", "user-input-e2e")
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _turn(*, final: bool) -> dict[str, Any]:
        status = "completed" if final else "waiting_for_user_input"
        return {
            "id": TURN_ID,
            "conversation_id": CONVERSATION_ID,
            "run_id": RUN_ID,
            "run_status": status,
            "run_revision": 5 if final else 4,
            "status": status,
            "sequence": 1,
            "user_input": "Handle the ambiguous object",
            "assistant_output": (
                {"answer": "authoritative result after one durable answer"} if final else None
            ),
            "error": None,
        }

    @classmethod
    def _queue(cls) -> dict[str, Any]:
        turn = cls._turn(final=STATE.answered.is_set())
        return {
            "conversation_id": CONVERSATION_ID,
            "revision": 2,
            "state": "active",
            "pause_reason": None,
            "head_turn": turn,
            "active_turn": turn,
            "pause_turn": None,
            "queued_turns": [turn],
            "queued_count": 1,
            "capacity": 20,
        }

    @staticmethod
    def _request() -> dict[str, Any]:
        return {
            "id": REQUEST_ID,
            "run_id": RUN_ID,
            "agent_action_id": "88888888-8888-4888-8888-888888888888",
            "question": "Which exact target should the Agent use?",
            "reason": "File and database remain equally plausible.",
            "input_schema": {
                "type": "string",
                "enum": ["file", "database"],
            },
            "status": "answered" if STATE.answered.is_set() else "requested",
            "answer_hash": "a" * 64 if STATE.answered.is_set() else None,
            "answer_ref": f"user_input:{REQUEST_ID}:answer" if STATE.answered.is_set() else None,
            "expires_at": "2099-01-01T00:00:00Z",
            "answered_at": "2026-07-23T00:00:00Z" if STATE.answered.is_set() else None,
            "revision": 2 if STATE.answered.is_set() else 1,
            "created_at": "2026-07-23T00:00:00Z",
            "updated_at": "2026-07-23T00:00:00Z",
        }

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/__state":
            self._json(STATE.snapshot())
            return
        if path == f"/api/v1/conversations/{CONVERSATION_ID}":
            self._json(
                {
                    "id": CONVERSATION_ID,
                    "project_id": None,
                    "agent_id": AGENT_ID,
                    "agent_version_id": VERSION_ID,
                    "mode": "personal",
                    "title": "UserInput restart proof",
                    "status": "active",
                    "approval_mode": "ask",
                    "revision": 1,
                    "last_turn_id": TURN_ID,
                }
            )
            return
        if path == f"/api/v1/conversations/{CONVERSATION_ID}/queue":
            self._json(self._queue())
            return
        if path == f"/api/v1/agents/{AGENT_ID}":
            self._json(
                {
                    "id": AGENT_ID,
                    "name": "fixture-agent",
                    "display_name": "Fixture Agent",
                    "show_response_metrics": False,
                }
            )
            return
        if path == f"/api/v1/agents/{AGENT_ID}/versions":
            self._json(
                [
                    {
                        "id": VERSION_ID,
                        "version": 1,
                        "runtime_provider": "nico_native",
                        "model_name": "explicit-action-fixture",
                        "tool_policy": {},
                    }
                ]
            )
            return
        if path == f"/api/v1/runs/{RUN_ID}/runtime":
            self._json(
                {
                    "execution_manifest": {
                        "model": "explicit-action-fixture",
                        "tool_approval_policy": {"mode": "ask"},
                    }
                }
            )
            return
        if path == "/api/v1/tool-approval-requests":
            self._json([])
            return
        if path == "/api/v1/user-input-requests":
            self._json([] if STATE.answered.is_set() else [self._request()])
            return
        if path == f"/api/v1/user-input-requests/{REQUEST_ID}":
            self._json(self._request())
            return
        if path == f"/api/v1/conversation-turns/{TURN_ID}":
            with STATE.lock:
                STATE.final_read_count += int(STATE.answered.is_set())
            self._json(self._turn(final=STATE.answered.is_set()))
            return
        if path == f"/api/v1/runs/{RUN_ID}/events/stream":
            STATE.answered.wait(timeout=20)
            payload = json.dumps(
                {
                    "sequence": 1,
                    "type": "RunCompleted",
                    "payload": {},
                },
                separators=(",", ":"),
            )
            body = f"id: 1\nevent: RunCompleted\ndata: {payload}\n\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass
            return
        self._json({"code": "RESOURCE_NOT_FOUND", "message": "fixture route missing"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        if path == f"/api/v1/user-input-requests/{REQUEST_ID}/answer":
            if payload != {"expected_revision": 1, "answer": "database"}:
                self._json(
                    {
                        "code": "USER_INPUT_ANSWER_INVALID",
                        "message": "fixture expected the database choice",
                    },
                    status=400,
                )
                return
            with STATE.lock:
                STATE.answer_count += 1
                STATE.answered.set()
            self._json(self._request())
            return
        if path == f"/api/v1/conversations/{CONVERSATION_ID}/turns":
            with STATE.lock:
                STATE.turn_create_count += 1
            self._json({"code": "UNEXPECTED_TURN", "message": "answer became a Turn"}, status=409)
            return
        self._json({"code": "RESOURCE_NOT_FOUND", "message": "fixture route missing"}, status=404)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
