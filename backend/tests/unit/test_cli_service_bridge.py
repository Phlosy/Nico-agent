from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from nico_agent.cli.config import ResolvedProfile
from nico_agent.cli.errors import CliError
from nico_agent.cli.service_bridge import ServiceBridge


def _profile(root: Path) -> ResolvedProfile:
    return ResolvedProfile(
        name="local",
        base_url="http://localhost:18000",
        tenant_id=None,
        actor_id="test",
        api_token_env=None,
        api_token=None,
        timeout_seconds=30,
        verify_tls=True,
        service_command=str(root / "bin/nico-service"),
        install_root=str(root),
    )


def _installation(tmp_path: Path) -> Path:
    root = tmp_path / "nico"
    (root / "bin").mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    command = root / "bin/nico-service"
    command.write_text("#!/bin/sh\n", encoding="utf-8")
    command.chmod(0o700)
    return root


def test_bridge_uses_stdin_and_fixed_argv_without_rendering_secret(tmp_path, monkeypatch) -> None:
    root = _installation(tmp_path)
    captured = {}

    def fake_run(argv, **kwargs):
        captured.update({"argv": argv, **kwargs})
        request = json.loads(kwargs["input"])
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "attempt_id": request["attempt_id"],
                    "token": "token-" + "x" * 32,
                    "credential_ref": f"env:{request['env_name']}",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr("subprocess.run", fake_run)
    attempt = ServiceBridge(_profile(root)).begin(
        "NICO_MODEL_SECRET_TEST",
        "sk-canary-value",
    )

    assert captured["argv"] == [str(root / "bin/nico-service"), "provider-secret", "begin"]
    assert captured["shell"] is False
    assert "sk-canary-value" not in " ".join(captured["argv"])
    assert captured["env"]["NICO_HOME"] == str(root)
    assert "sk-canary-value" not in repr(attempt)


def test_bridge_routes_tool_credentials_through_generic_transaction(tmp_path, monkeypatch) -> None:
    root = _installation(tmp_path)
    captured = {}

    def fake_run(argv, **kwargs):
        captured.update({"argv": argv, **kwargs})
        request = json.loads(kwargs["input"])
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "attempt_id": request["attempt_id"],
                    "token": "token-" + "x" * 32,
                    "credential_ref": f"env:{request['env_name']}",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr("subprocess.run", fake_run)
    attempt = ServiceBridge(_profile(root)).begin_credential(
        "NICO_TOOL_SECRET_WEB_SEARCH_BRAVE_TEST",
        "brave-canary",
    )

    assert captured["argv"] == [
        str(root / "bin/nico-service"),
        "credential-secret",
        "begin",
    ]
    assert attempt.command == "credential-secret"
    assert "brave-canary" not in " ".join(captured["argv"])
    assert "brave-canary" not in repr(attempt)


def test_bridge_checks_tool_credential_without_returning_its_value(tmp_path, monkeypatch) -> None:
    root = _installation(tmp_path)
    captured = {}

    def fake_run(argv, **kwargs):
        captured.update({"argv": argv, **kwargs})
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"ok": True, "available": True}),
            stderr="",
        )

    monkeypatch.setattr("subprocess.run", fake_run)

    assert ServiceBridge(_profile(root)).credential_available(
        "env:NICO_TOOL_SECRET_WEB_SEARCH_BRAVE_TEST"
    )
    assert captured["argv"] == [
        str(root / "bin/nico-service"),
        "credential-secret",
        "check",
    ]
    assert json.loads(captured["input"]) == {
        "credential_ref": "env:NICO_TOOL_SECRET_WEB_SEARCH_BRAVE_TEST"
    }


def test_bridge_rejects_symlink_and_writable_install_paths(tmp_path: Path) -> None:
    root = _installation(tmp_path)
    command = root / "bin/nico-service"
    target = root / "bin/target"
    command.rename(target)
    command.symlink_to(target)

    with pytest.raises(CliError) as captured:
        ServiceBridge(_profile(root)).recover()
    assert captured.value.code == "LOCAL_SERVICE_ATTESTATION_FAILED"

    command.unlink()
    target.rename(command)
    (root / "bin").chmod(0o722)
    with pytest.raises(CliError) as writable:
        ServiceBridge(_profile(root)).recover()
    assert writable.value.code == "LOCAL_SERVICE_ATTESTATION_FAILED"


def test_bridge_reports_missing_local_profile_without_subprocess() -> None:
    profile = _profile(Path("/tmp") / uuid4().hex).model_copy(
        update={"service_command": None, "install_root": None}
    )
    with pytest.raises(CliError) as captured:
        ServiceBridge(profile).recover()
    assert captured.value.code == "LOCAL_SERVICE_UNAVAILABLE"


def test_bridge_bounds_service_subprocess_time(tmp_path: Path, monkeypatch) -> None:
    root = _installation(tmp_path)

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["nico-service"], 45)

    monkeypatch.setattr("subprocess.run", timeout)

    with pytest.raises(CliError) as captured:
        ServiceBridge(_profile(root)).recover()

    assert captured.value.code == "LOCAL_SERVICE_UNAVAILABLE"
