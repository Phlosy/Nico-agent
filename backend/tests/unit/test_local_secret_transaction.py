from __future__ import annotations

import json
import stat
from pathlib import Path
from uuid import uuid4

import pytest

from nico_agent.local_secret_transaction import LocalSecretError, LocalSecretTransaction


class FakeTransaction(LocalSecretTransaction):
    def __init__(self, home: Path) -> None:
        super().__init__(home)
        self.restarts = 0
        self.control_actions: list[str] = []

    def _local_control(self, action, payload, *, allow_failure):
        self.control_actions.append(action)
        if action == "acquire":
            return {
                "ok": True,
                "attempt_id": payload["attempt_id"],
                "token": "maintenance-token-" + "x" * 32,
            }
        return {"ok": True}

    def _restart_native_worker(self) -> None:
        self.restarts += 1


@pytest.fixture
def transaction(tmp_path: Path) -> FakeTransaction:
    home = tmp_path / "nico"
    (home / "current").mkdir(parents=True)
    (home / "config").mkdir(mode=0o700)
    (home / "config/deployment.env").write_text("NICO_RUNTIME=native\n", encoding="utf-8")
    return FakeTransaction(home)


def test_secret_transaction_commits_without_copying_secret_to_journal(
    transaction: FakeTransaction,
) -> None:
    attempt_id = str(uuid4())
    response = transaction.execute(
        "begin",
        {
            "attempt_id": attempt_id,
            "env_name": "NICO_MODEL_SECRET_OPENAI_TEST",
            "secret": "sk-canary-value",
        },
    )

    assert response["credential_ref"] == "env:NICO_MODEL_SECRET_OPENAI_TEST"
    assert "sk-canary-value" in transaction.secret_file.read_text()
    assert "sk-canary-value" not in transaction.journal_file.read_text()
    assert stat.S_IMODE(transaction.secret_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(transaction.journal_file.stat().st_mode) == 0o600
    assert transaction.restarts == 1

    transaction.execute(
        "commit",
        {"attempt_id": attempt_id, "token": response["token"]},
    )

    assert not transaction.journal_file.exists()
    assert "sk-canary-value" in transaction.secret_file.read_text()
    assert transaction.control_actions == ["acquire", "release"]


def test_rollback_removes_only_attempt_owned_secret(transaction: FakeTransaction) -> None:
    transaction.secret_file.write_text("NICO_MODEL_SECRET_EXISTING=keep\n", encoding="utf-8")
    transaction.secret_file.chmod(0o600)
    attempt_id = str(uuid4())
    response = transaction.execute(
        "begin",
        {
            "attempt_id": attempt_id,
            "env_name": "NICO_MODEL_SECRET_ROTATION_2",
            "secret": "remove-me",
        },
    )

    transaction.execute(
        "rollback",
        {"attempt_id": attempt_id, "token": response["token"]},
    )

    values = transaction.secret_file.read_text()
    assert "NICO_MODEL_SECRET_EXISTING=keep" in values
    assert "remove-me" not in values
    assert transaction.restarts == 2
    assert not transaction.journal_file.exists()


@pytest.mark.parametrize("secret", ["", "one\ntwo", "one\rtwo", "x" * 8193])
def test_secret_transaction_rejects_invalid_values(
    transaction: FakeTransaction,
    secret: str,
) -> None:
    with pytest.raises(LocalSecretError, match="single-line"):
        transaction.execute(
            "begin",
            {
                "attempt_id": str(uuid4()),
                "env_name": "NICO_MODEL_SECRET_INVALID_TEST",
                "secret": secret,
            },
        )
    assert not transaction.journal_file.exists()


def test_recovery_rolls_back_staged_secret(transaction: FakeTransaction) -> None:
    attempt_id = str(uuid4())
    transaction.execute(
        "begin",
        {
            "attempt_id": attempt_id,
            "env_name": "NICO_MODEL_SECRET_RECOVER_TEST",
            "secret": "recover-canary",
        },
    )

    recovered = transaction.execute("recover", {})

    assert recovered == {"ok": True, "recovered": True, "rolled_back": True}
    assert "recover-canary" not in transaction.secret_file.read_text()
    assert not transaction.journal_file.exists()


def test_journal_contains_only_expected_safe_fields(transaction: FakeTransaction) -> None:
    transaction.execute(
        "begin",
        {
            "attempt_id": str(uuid4()),
            "env_name": "NICO_MODEL_SECRET_FIELDS_TEST",
            "secret": "field-canary",
        },
    )
    journal = json.loads(transaction.journal_file.read_text())
    assert set(journal) == {"schema_version", "attempt_id", "token", "env_name", "phase"}


def test_secret_transaction_rejects_writable_configuration_directory(
    transaction: FakeTransaction,
) -> None:
    transaction.config_dir.chmod(0o770)

    with pytest.raises(LocalSecretError, match="owner-only"):
        transaction.execute("recover", {})
