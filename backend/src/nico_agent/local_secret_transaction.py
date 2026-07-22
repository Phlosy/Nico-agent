"""Recoverable host-side model/tool credential transaction used by nico-service."""

from __future__ import annotations

import hmac
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from uuid import UUID

_ENV_NAME = re.compile(r"^NICO_(?:MODEL|TOOL)_SECRET_[A-Z0-9_]{1,100}$")
_MAINTENANCE_LEASE_SECONDS = 300
_MAINTENANCE_RENEW_INTERVAL_SECONDS = 30
_WORKER_RESTART_TIMEOUT_SECONDS = 120
_CONTROL_TIMEOUT_SECONDS = 30
_INSPECT_TIMEOUT_SECONDS = 10


class LocalSecretError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class LocalSecretTransaction:
    def __init__(self, home: Path) -> None:
        self.home = home.resolve()
        self.current = self.home / "current"
        self.config_dir = self.home / "config"
        self.deployment_env = self.config_dir / "deployment.env"
        self.secret_file = self.config_dir / "model-secrets.env"
        self.journal_file = self.config_dir / "provider-secret-attempt.json"

    def execute(self, action: str, request: dict[str, Any]) -> dict[str, Any]:
        self._require_installation()
        if action == "begin":
            return self._begin(request)
        if action == "renew":
            return self._renew(request)
        if action == "commit":
            return self._commit(request)
        if action == "rollback":
            return self._rollback(request)
        if action == "recover":
            return self._recover()
        raise LocalSecretError("LOCAL_SECRET_INVALID", "unknown Provider secret action")

    def _begin(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.journal_file.exists():
            raise LocalSecretError(
                "LOCAL_SECRET_RECOVERY_REQUIRED",
                "recover the previous Provider secret attempt first",
            )
        attempt_id = self._attempt_id(request)
        env_name = request.get("env_name")
        secret = request.get("secret")
        if not isinstance(env_name, str) or _ENV_NAME.fullmatch(env_name) is None:
            raise LocalSecretError(
                "LOCAL_SECRET_INVALID",
                "credential environment name is invalid",
            )
        if (
            not isinstance(secret, str)
            or not secret
            or len(secret) > 8192
            or "\n" in secret
            or "\r" in secret
            or "\x00" in secret
        ):
            raise LocalSecretError(
                "LOCAL_SECRET_INVALID",
                "credential must be a non-empty single-line value up to 8192 characters",
            )
        if env_name in self._read_env(self.secret_file):
            raise LocalSecretError(
                "LOCAL_SECRET_EXISTS",
                "Provider secret names are immutable; create a new rotation name",
            )
        acquired = self._local_control(
            "acquire",
            {
                "attempt_id": str(attempt_id),
                "lease_seconds": _MAINTENANCE_LEASE_SECONDS,
            },
            allow_failure=True,
        )
        if not acquired.get("ok"):
            return acquired
        token = acquired.get("token")
        if not isinstance(token, str) or len(token) < 32:
            raise LocalSecretError(
                "LOCAL_CONTROL_INVALID",
                "maintenance acquisition returned an invalid capability",
            )
        journal = {
            "schema_version": 1,
            "attempt_id": str(attempt_id),
            "token": token,
            "env_name": env_name,
            "kind": "tool" if env_name.startswith("NICO_TOOL_SECRET_") else "model",
            "phase": "acquired",
        }
        self._write_json(self.journal_file, journal)
        try:
            values = self._read_env(self.secret_file)
            values[env_name] = secret
            self._write_env(self.secret_file, values)
            journal["phase"] = "staged"
            self._write_json(self.journal_file, journal)
            self._restart_native_worker(journal)
        except Exception:
            self._rollback_journal(journal, strict=False)
            raise
        finally:
            secret = ""
        return {
            "ok": True,
            "attempt_id": str(attempt_id),
            "token": token,
            "credential_ref": f"env:{env_name}",
        }

    def _renew(self, request: dict[str, Any]) -> dict[str, Any]:
        journal = self._required_journal(request)
        self._renew_journal(journal)
        return {"ok": True, "attempt_id": journal["attempt_id"]}

    def _renew_journal(self, journal: dict[str, Any]) -> None:
        response = self._local_control(
            "renew",
            {
                "attempt_id": journal["attempt_id"],
                "token": journal["token"],
                "lease_seconds": _MAINTENANCE_LEASE_SECONDS,
            },
            allow_failure=True,
        )
        if not response.get("ok"):
            raise LocalSecretError(
                "RUNTIME_MAINTENANCE_LEASE_LOST",
                "Provider maintenance lease could not be renewed",
            )

    def _commit(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self.journal_file.exists():
            return {"ok": True, "committed": True, "already_final": True}
        journal = self._required_journal(request)
        journal["phase"] = "committing"
        self._write_json(self.journal_file, journal)
        released = self._local_control(
            "release",
            {"attempt_id": journal["attempt_id"], "token": journal["token"]},
            allow_failure=True,
        )
        if not released.get("ok"):
            raise LocalSecretError(
                "RUNTIME_MAINTENANCE_LEASE_LOST",
                "Provider maintenance lease could not be released",
            )
        self.journal_file.unlink(missing_ok=True)
        return {"ok": True, "committed": True, "credential_ref": f"env:{journal['env_name']}"}

    def _rollback(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self.journal_file.exists():
            return {"ok": True, "rolled_back": True, "already_final": True}
        journal = self._required_journal(request)
        self._rollback_journal(journal, strict=True)
        return {"ok": True, "rolled_back": True}

    def _recover(self) -> dict[str, Any]:
        if not self.journal_file.exists():
            return {"ok": True, "recovered": False}
        journal = self._load_journal()
        if journal["phase"] == "committing":
            self._local_control(
                "release",
                {"attempt_id": journal["attempt_id"], "token": journal["token"]},
                allow_failure=True,
            )
            self.journal_file.unlink(missing_ok=True)
            return {"ok": True, "recovered": True, "committed": True}
        self._rollback_journal(journal, strict=False)
        return {"ok": True, "recovered": True, "rolled_back": True}

    def _rollback_journal(self, journal: dict[str, Any], *, strict: bool) -> None:
        values = self._read_env(self.secret_file)
        removed = values.pop(journal["env_name"], None) is not None
        self._write_env(self.secret_file, values)
        restart_error: Exception | None = None
        if removed:
            try:
                self._restart_native_worker(journal)
            except Exception as exc:
                restart_error = exc
        released = self._local_control(
            "release",
            {"attempt_id": journal["attempt_id"], "token": journal["token"]},
            allow_failure=True,
        )
        if restart_error is None and (released.get("ok") or not strict):
            self.journal_file.unlink(missing_ok=True)
        if restart_error is not None:
            if (
                isinstance(restart_error, LocalSecretError)
                and restart_error.code == "RUNTIME_MAINTENANCE_LEASE_LOST"
            ):
                raise restart_error
            raise LocalSecretError(
                "LOCAL_SECRET_WORKER_UNHEALTHY",
                "Native Worker did not recover after Provider secret rollback",
            ) from restart_error
        if strict and not released.get("ok"):
            raise LocalSecretError(
                "RUNTIME_MAINTENANCE_LEASE_LOST",
                "Provider maintenance lease could not be released",
            )

    def _required_journal(self, request: dict[str, Any]) -> dict[str, Any]:
        journal = self._load_journal()
        attempt_id = str(self._attempt_id(request))
        token = request.get("token")
        if (
            not isinstance(token, str)
            or attempt_id != journal["attempt_id"]
            or not hmac.compare_digest(token, journal["token"])
        ):
            raise LocalSecretError(
                "LOCAL_SECRET_CAPABILITY_MISMATCH",
                "Provider secret transaction capability does not match",
            )
        return journal

    def _load_journal(self) -> dict[str, Any]:
        self._secure_regular_file(self.journal_file)
        try:
            value = json.loads(self.journal_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LocalSecretError(
                "LOCAL_SECRET_JOURNAL_INVALID",
                "Provider secret recovery journal is invalid",
            ) from exc
        required = {"attempt_id", "token", "env_name", "phase"}
        if not isinstance(value, dict) or not required <= value.keys():
            raise LocalSecretError(
                "LOCAL_SECRET_JOURNAL_INVALID",
                "Provider secret recovery journal is invalid",
            )
        return value

    def _local_control(
        self,
        action: str,
        payload: dict[str, Any],
        *,
        allow_failure: bool,
    ) -> dict[str, Any]:
        try:
            completed = subprocess.run(
                [
                    *self._compose_command(),
                    "exec",
                    "-T",
                    "api",
                    "python",
                    "-m",
                    "nico_agent.local_control",
                    action,
                ],
                input=json.dumps(payload, separators=(",", ":")),
                text=True,
                capture_output=True,
                check=False,
                timeout=_CONTROL_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise LocalSecretError(
                "LOCAL_CONTROL_UNAVAILABLE",
                "local maintenance control timed out",
            ) from exc
        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise LocalSecretError(
                "LOCAL_CONTROL_UNAVAILABLE",
                "local maintenance control returned an invalid response",
            ) from exc
        if not isinstance(response, dict):
            raise LocalSecretError(
                "LOCAL_CONTROL_UNAVAILABLE",
                "local maintenance control returned an invalid response",
            )
        if completed.returncode and not allow_failure:
            raise LocalSecretError(
                str(response.get("code") or "LOCAL_CONTROL_UNAVAILABLE"),
                str(response.get("message") or "local maintenance control failed"),
            )
        return response

    def _restart_native_worker(self, journal: dict[str, Any]) -> None:
        self._renew_journal(journal)
        try:
            completed = subprocess.run(
                [
                    *self._compose_command(),
                    "--profile",
                    "native",
                    "up",
                    "--detach",
                    "--no-deps",
                    "--force-recreate",
                    "worker",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=_WORKER_RESTART_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise LocalSecretError(
                "LOCAL_SECRET_WORKER_UNHEALTHY",
                "Native Worker recreation timed out",
            ) from exc
        if completed.returncode:
            raise LocalSecretError(
                "LOCAL_SECRET_WORKER_UNHEALTHY",
                "Native Worker recreation failed",
            )
        deadline = time.monotonic() + _WORKER_RESTART_TIMEOUT_SECONDS
        renew_at = time.monotonic() + _MAINTENANCE_RENEW_INTERVAL_SECONDS
        while time.monotonic() < deadline:
            if time.monotonic() >= renew_at:
                self._renew_journal(journal)
                renew_at = time.monotonic() + _MAINTENANCE_RENEW_INTERVAL_SECONDS
            container = subprocess.run(
                [*self._compose_command(), "ps", "--quiet", "worker"],
                capture_output=True,
                text=True,
                check=False,
                timeout=_INSPECT_TIMEOUT_SECONDS,
            ).stdout.strip()
            if container:
                status_result = subprocess.run(
                    ["docker", "inspect", "--format", "{{.State.Health.Status}}", container],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=_INSPECT_TIMEOUT_SECONDS,
                )
                status = status_result.stdout.strip()
                if status_result.returncode == 0 and status == "healthy":
                    return
                if status == "unhealthy":
                    break
            time.sleep(1)
        raise LocalSecretError(
            "LOCAL_SECRET_WORKER_UNHEALTHY",
            "Native Worker did not become healthy after Provider secret update",
        )

    def _compose_command(self) -> list[str]:
        return [
            "docker",
            "compose",
            "--env-file",
            str(self.deployment_env),
            "--project-directory",
            str(self.current),
            "--file",
            str(self.current / "docker-compose.yml"),
            "--file",
            str(self.current / "deploy/docker-compose.release.yml"),
        ]

    def _require_installation(self) -> None:
        if not self.current.exists() or not self.deployment_env.is_file():
            raise LocalSecretError("LOCAL_SECRET_UNAVAILABLE", "Nico installation is incomplete")
        self.config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            config_info = self.config_dir.lstat()
        except OSError as exc:
            raise LocalSecretError(
                "LOCAL_SECRET_UNAVAILABLE",
                "the Nico configuration directory is unavailable",
            ) from exc
        if (
            not stat.S_ISDIR(config_info.st_mode)
            or config_info.st_uid != os.getuid()
            or stat.S_IMODE(config_info.st_mode) & 0o077
        ):
            raise LocalSecretError(
                "LOCAL_SECRET_PERMISSIONS",
                "the Nico configuration directory must be owner-only",
            )
        if self._read_env(self.deployment_env).get("NICO_RUNTIME") != "native":
            raise LocalSecretError(
                "LOCAL_SECRET_NATIVE_REQUIRED",
                "Provider onboarding configures the Nico Native Worker only",
            )
        if self.secret_file.exists():
            self._secure_regular_file(self.secret_file)
        else:
            self._write_env(self.secret_file, {})

    def _secure_regular_file(self, path: Path) -> None:
        try:
            info = path.lstat()
        except OSError as exc:
            raise LocalSecretError(
                "LOCAL_SECRET_UNAVAILABLE",
                f"required file is unavailable: {path.name}",
            ) from exc
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise LocalSecretError(
                "LOCAL_SECRET_PERMISSIONS",
                f"{path.name} must be an owner-only regular file",
            )

    @staticmethod
    def _attempt_id(request: dict[str, Any]) -> UUID:
        value = request.get("attempt_id")
        if not isinstance(value, str):
            raise LocalSecretError("LOCAL_SECRET_INVALID", "attempt_id is required")
        try:
            return UUID(value)
        except ValueError as exc:
            raise LocalSecretError("LOCAL_SECRET_INVALID", "attempt_id is invalid") from exc

    @staticmethod
    def _read_env(path: Path) -> dict[str, str]:
        if not path.exists():
            return {}
        values: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            if separator and re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
                values[key] = value
        return values

    def _write_env(self, path: Path, values: dict[str, str]) -> None:
        content = "".join(f"{key}={values[key]}\n" for key in sorted(values))
        self._atomic_write(path, content)

    def _write_json(self, path: Path, value: dict[str, Any]) -> None:
        self._atomic_write(path, json.dumps(value, separators=(",", ":"), sort_keys=True))

    def _atomic_write(self, path: Path, content: str) -> None:
        self.config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=self.config_dir)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            temporary.unlink(missing_ok=True)


def run() -> None:
    action = sys.argv[1] if len(sys.argv) == 2 else ""
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise LocalSecretError("LOCAL_SECRET_INVALID", "request must be a JSON object")
        home = Path(os.environ.get("NICO_HOME", "~/.nico")).expanduser()
        response = LocalSecretTransaction(home).execute(action, request)
    except (json.JSONDecodeError, LocalSecretError) as exc:
        code = exc.code if isinstance(exc, LocalSecretError) else "LOCAL_SECRET_INVALID"
        message = exc.message if isinstance(exc, LocalSecretError) else "request is invalid JSON"
        response = {"ok": False, "code": code, "message": message}
    except Exception:
        response = {
            "ok": False,
            "code": "LOCAL_SECRET_UNAVAILABLE",
            "message": "Provider secret transaction failed",
        }
    print(json.dumps(response, separators=(",", ":"), sort_keys=True))
    if not response.get("ok"):
        raise SystemExit(4)


if __name__ == "__main__":
    run()
