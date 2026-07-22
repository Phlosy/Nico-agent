"""Attested, stdin-only bridge to the installed Nico service helper."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from nico_agent.cli.config import ResolvedProfile
from nico_agent.cli.errors import CliError


@dataclass(frozen=True, slots=True)
class SecretAttempt:
    attempt_id: str
    token: str = field(repr=False)
    credential_ref: str
    command: str = "provider-secret"


class ServiceBridge:
    def __init__(self, profile: ResolvedProfile) -> None:
        self.profile = profile

    def recover(self) -> dict[str, Any]:
        return self._invoke("recover", {})

    def begin(self, env_name: str, secret: str) -> SecretAttempt:
        return self._begin("provider-secret", env_name, secret)

    def recover_credentials(self) -> dict[str, Any]:
        return self._invoke("recover", {}, command="credential-secret")

    def begin_credential(self, env_name: str, secret: str) -> SecretAttempt:
        return self._begin("credential-secret", env_name, secret)

    def credential_available(self, credential_ref: str) -> bool:
        response = self._invoke(
            "check",
            {"credential_ref": credential_ref},
            command="credential-secret",
        )
        return response.get("available") is True

    def _begin(self, command: str, env_name: str, secret: str) -> SecretAttempt:
        attempt_id = str(uuid4())
        response = self._invoke(
            "begin",
            {"attempt_id": attempt_id, "env_name": env_name, "secret": secret},
            command=command,
        )
        try:
            return SecretAttempt(
                attempt_id=str(response["attempt_id"]),
                token=str(response["token"]),
                credential_ref=str(response["credential_ref"]),
                command=command,
            )
        except KeyError as exc:
            raise CliError(
                "LOCAL_SERVICE_INVALID",
                "the local service returned an incomplete secret transaction",
                exit_code=3,
            ) from exc

    def renew(self, attempt: SecretAttempt) -> None:
        self._invoke("renew", self._capability(attempt), command=attempt.command)

    def commit(self, attempt: SecretAttempt) -> None:
        self._invoke("commit", self._capability(attempt), command=attempt.command)

    def rollback(self, attempt: SecretAttempt) -> None:
        self._invoke("rollback", self._capability(attempt), command=attempt.command)

    @staticmethod
    def _capability(attempt: SecretAttempt) -> dict[str, str]:
        return {"attempt_id": attempt.attempt_id, "token": attempt.token}

    def _invoke(
        self,
        action: str,
        payload: dict[str, Any],
        *,
        command: str = "provider-secret",
    ) -> dict[str, Any]:
        executable, install_root = self._attested_command()
        environment = dict(os.environ)
        environment["NICO_HOME"] = str(install_root)
        try:
            completed = subprocess.run(
                [str(executable), command, action],
                input=json.dumps(payload, separators=(",", ":")),
                text=True,
                capture_output=True,
                env=environment,
                shell=False,
                check=False,
                timeout=360 if action == "begin" else 45,
            )
        except subprocess.TimeoutExpired as exc:
            raise CliError(
                "LOCAL_SERVICE_UNAVAILABLE",
                "the local Nico service operation timed out",
                exit_code=3,
            ) from exc
        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise CliError(
                "LOCAL_SERVICE_UNAVAILABLE",
                "the local Nico service returned an invalid response",
                exit_code=3,
            ) from exc
        if not isinstance(response, dict):
            raise CliError(
                "LOCAL_SERVICE_UNAVAILABLE",
                "the local Nico service returned an invalid response",
                exit_code=3,
            )
        if completed.returncode or not response.get("ok"):
            code = str(response.get("code") or "LOCAL_SERVICE_UNAVAILABLE")
            exit_code = 4 if code.startswith(("LOCAL_SECRET_", "RUNTIME_MAINTENANCE_")) else 3
            raise CliError(
                code,
                str(response.get("message") or "the local Nico service operation failed"),
                exit_code=exit_code,
            )
        return response

    def _attested_command(self) -> tuple[Path, Path]:
        if not self.profile.service_command or not self.profile.install_root:
            raise CliError(
                "LOCAL_SERVICE_UNAVAILABLE",
                "this profile is not linked to a local Nico installation",
                exit_code=3,
            )
        command = Path(self.profile.service_command)
        install_root = Path(self.profile.install_root)
        expected = install_root / "bin/nico-service"
        if command != expected:
            raise CliError(
                "LOCAL_SERVICE_ATTESTATION_FAILED",
                "the profile service command is outside the expected installation path",
                exit_code=4,
            )
        self._attest_path(install_root, directory=True)
        self._attest_path(install_root / "bin", directory=True)
        self._attest_path(command, directory=False)
        return command, install_root

    @staticmethod
    def _attest_path(path: Path, *, directory: bool) -> None:
        try:
            info = path.lstat()
        except OSError as exc:
            raise CliError(
                "LOCAL_SERVICE_UNAVAILABLE",
                "the installed Nico service command is unavailable",
                exit_code=3,
            ) from exc
        expected_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if (
            not expected_type
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o022
            or (not directory and info.st_nlink != 1)
        ):
            raise CliError(
                "LOCAL_SERVICE_ATTESTATION_FAILED",
                "the installed Nico service path failed owner and permission checks",
                exit_code=4,
            )
