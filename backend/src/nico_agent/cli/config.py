"""Local profile storage with environment-only secret resolution."""

from __future__ import annotations

import json
import os
import re
import stat
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from platformdirs import user_config_path
from pydantic import BaseModel, ConfigDict, Field, field_validator

from nico_agent.cli.errors import CliError

PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DEFAULT_PROFILE = "default"
DEFAULT_API_URL = "http://localhost:18000"


class Profile(BaseModel):
    """Non-secret connection settings stored on disk."""

    model_config = ConfigDict(frozen=True)

    base_url: str = DEFAULT_API_URL
    tenant_id: UUID | None = None
    actor_id: str = Field(default="nico-cli", min_length=1, max_length=200)
    api_token_env: str | None = None
    timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    verify_tls: bool = True

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        try:
            parsed = httpx.URL(normalized)
        except Exception as exc:
            raise ValueError("base_url must be a valid HTTP(S) URL") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.host:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        if parsed.query or parsed.fragment:
            raise ValueError("base_url cannot contain a query or fragment")
        return normalized

    @field_validator("api_token_env")
    @classmethod
    def validate_token_env(cls, value: str | None) -> str | None:
        if value is not None and not ENV_NAME_RE.fullmatch(value):
            raise ValueError("api_token_env must be an environment variable name")
        return value


class CliConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    current_profile: str = DEFAULT_PROFILE
    profiles: dict[str, Profile] = Field(default_factory=lambda: {DEFAULT_PROFILE: Profile()})

    @field_validator("current_profile")
    @classmethod
    def validate_current_profile(cls, value: str) -> str:
        validate_profile_name(value)
        return value

    @field_validator("profiles")
    @classmethod
    def validate_profiles(cls, value: dict[str, Profile]) -> dict[str, Profile]:
        if not value:
            raise ValueError("at least one profile is required")
        for name in value:
            validate_profile_name(name)
        return value


class ResolvedProfile(BaseModel):
    """Effective profile plus a token resolved only in memory."""

    model_config = ConfigDict(frozen=True)

    name: str
    base_url: str
    tenant_id: UUID | None
    actor_id: str
    api_token_env: str | None
    api_token: str | None = Field(default=None, exclude=True, repr=False)
    timeout_seconds: float
    verify_tls: bool

    def public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "base_url": self.base_url,
            "tenant_id": str(self.tenant_id) if self.tenant_id else None,
            "actor_id": self.actor_id,
            "api_token_env": self.api_token_env,
            "api_token_available": self.api_token is not None,
            "timeout_seconds": self.timeout_seconds,
            "verify_tls": self.verify_tls,
        }


def default_config_path() -> Path:
    return user_config_path("nico", appauthor=False) / "config.toml"


def validate_profile_name(name: str) -> None:
    if not PROFILE_NAME_RE.fullmatch(name):
        raise CliError(
            "INVALID_PROFILE_NAME",
            "profile names may contain letters, numbers, dot, underscore and dash",
            exit_code=2,
        )


class ConfigStore:
    def __init__(
        self,
        path: Path | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        source = os.environ if environ is None else environ
        configured_path = source.get("NICO_CONFIG_FILE")
        self.path = path or (
            Path(configured_path).expanduser() if configured_path else default_config_path()
        )
        self.environ = dict(source)

    def load(self) -> CliConfig:
        if not self.path.exists():
            return CliConfig()
        try:
            with self.path.open("rb") as handle:
                raw = tomllib.load(handle)
            config = CliConfig.model_validate(raw)
        except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
            raise CliError(
                "INVALID_CONFIG",
                f"cannot read Nico config at {self.path}: {exc}",
                exit_code=2,
            ) from exc
        if config.current_profile not in config.profiles:
            raise CliError(
                "PROFILE_NOT_FOUND",
                f"current profile '{config.current_profile}' does not exist",
                exit_code=2,
            )
        return config

    def save(self, config: CliConfig) -> None:
        if config.current_profile not in config.profiles:
            raise CliError(
                "PROFILE_NOT_FOUND",
                f"current profile '{config.current_profile}' does not exist",
                exit_code=2,
            )
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        temp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(_serialize(config))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
            os.chmod(self.path, 0o600)
        except OSError as exc:
            temp.unlink(missing_ok=True)
            raise CliError(
                "CONFIG_WRITE_FAILED",
                f"cannot write Nico config at {self.path}: {exc}",
                exit_code=2,
            ) from exc

    def resolve(
        self,
        *,
        profile_name: str | None = None,
        base_url: str | None = None,
        tenant_id: UUID | None = None,
        actor_id: str | None = None,
    ) -> ResolvedProfile:
        config = self.load()
        selected = profile_name or self.environ.get("NICO_PROFILE") or config.current_profile
        validate_profile_name(selected)
        profile = config.profiles.get(selected)
        if profile is None:
            raise CliError("PROFILE_NOT_FOUND", f"profile '{selected}' does not exist", exit_code=2)
        try:
            effective = profile.model_copy(
                update={
                    "base_url": base_url or self.environ.get("NICO_API_URL") or profile.base_url,
                    "tenant_id": tenant_id
                    or self.environ.get("NICO_TENANT_ID")
                    or profile.tenant_id,
                    "actor_id": actor_id or self.environ.get("NICO_ACTOR_ID") or profile.actor_id,
                }
            )
            effective = Profile.model_validate(effective.model_dump())
        except ValueError as exc:
            raise CliError("INVALID_CONFIG_OVERRIDE", str(exc), exit_code=2) from exc
        token = self.environ.get("NICO_API_TOKEN")
        if token is None and effective.api_token_env:
            token = self.environ.get(effective.api_token_env)
        return ResolvedProfile(
            name=selected,
            **effective.model_dump(),
            api_token=token or None,
        )

    def permissions(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"exists": False, "secure": True, "mode": None}
        mode = stat.S_IMODE(self.path.stat().st_mode)
        return {"exists": True, "secure": mode & 0o077 == 0, "mode": f"{mode:04o}"}


def _serialize(config: CliConfig) -> str:
    lines = [f"current_profile = {json.dumps(config.current_profile)}", ""]
    for name in sorted(config.profiles):
        profile = config.profiles[name]
        lines.append(f"[profiles.{json.dumps(name)}]")
        lines.append(f"base_url = {json.dumps(profile.base_url)}")
        if profile.tenant_id is not None:
            lines.append(f"tenant_id = {json.dumps(str(profile.tenant_id))}")
        lines.append(f"actor_id = {json.dumps(profile.actor_id)}")
        if profile.api_token_env is not None:
            lines.append(f"api_token_env = {json.dumps(profile.api_token_env)}")
        lines.append(f"timeout_seconds = {profile.timeout_seconds}")
        lines.append(f"verify_tls = {'true' if profile.verify_tls else 'false'}")
        lines.append("")
    return "\n".join(lines)
