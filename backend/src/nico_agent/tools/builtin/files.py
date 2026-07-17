"""Tenant/Run-scoped text file and report tools with descriptor-relative I/O."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import os
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from nico_agent.tools.contracts import (
    ToolDefinitionSpec,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolIsolation,
    ToolRetryPolicy,
    ToolRisk,
    canonical_hash,
    canonical_json,
)
from nico_agent.tools.errors import ToolExecutorFailure

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_READ_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
_LOCK_NAME = ".nico-workspace.lock"
_MAX_RELATIVE_PATH = 500
_MAX_SEGMENT = 120


class WorkspaceManager:
    def __init__(
        self,
        root: str | Path,
        *,
        max_file_bytes: int = 1_048_576,
        max_total_bytes: int = 10_485_760,
    ) -> None:
        if max_file_bytes < 1 or max_total_bytes < max_file_bytes:
            raise ValueError("workspace limits must be positive and total must cover one file")
        root_path = Path(root)
        root_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root_path.is_symlink():
            raise ValueError("workspace root cannot be a symbolic link")
        self.root = root_path.resolve(strict=True)
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self._locks: dict[tuple[UUID, UUID], threading.Lock] = {}
        self._locks_guard = threading.Lock()

    async def read_text(
        self,
        context: ToolExecutionContext,
        relative_path: str,
    ) -> dict[str, Any]:
        max_bytes = self._effective_limit(
            context.tool_config.get("max_file_bytes"), self.max_file_bytes
        )
        return await asyncio.to_thread(
            self._read_text,
            context.tenant_id,
            context.run_id,
            relative_path,
            max_bytes,
        )

    async def write_text(
        self,
        context: ToolExecutionContext,
        relative_path: str,
        content: str,
        *,
        overwrite: bool,
    ) -> dict[str, Any]:
        encoded = content.encode("utf-8")
        max_file_bytes = self._effective_limit(
            context.tool_config.get("max_file_bytes"), self.max_file_bytes
        )
        max_total_bytes = self._effective_limit(
            context.tool_config.get("max_total_bytes"), self.max_total_bytes
        )
        if len(encoded) > max_file_bytes:
            raise ToolExecutorFailure("FILE_TOO_LARGE", "file content exceeds its byte limit")
        return await asyncio.to_thread(
            self._write_bytes,
            context.tenant_id,
            context.run_id,
            relative_path,
            encoded,
            overwrite,
            max_total_bytes,
        )

    def _read_text(
        self,
        tenant_id: UUID,
        run_id: UUID,
        relative_path: str,
        max_bytes: int,
    ) -> dict[str, Any]:
        parts = _path_parts(relative_path)
        with self._workspace(tenant_id, run_id, exclusive=False) as workspace_fd:
            parent_fd = self._open_parent(workspace_fd, parts[:-1], create=False)
            try:
                try:
                    file_fd = os.open(parts[-1], _READ_FLAGS, dir_fd=parent_fd)
                except FileNotFoundError as exc:
                    raise ToolExecutorFailure(
                        "FILE_NOT_FOUND", "workspace file was not found"
                    ) from exc
                except OSError as exc:
                    raise ToolExecutorFailure(
                        "FILE_PATH_DENIED", "workspace path is not readable"
                    ) from exc
                try:
                    metadata = os.fstat(file_fd)
                    _require_private_regular_file(metadata)
                    if metadata.st_size > max_bytes:
                        raise ToolExecutorFailure(
                            "FILE_TOO_LARGE", "workspace file exceeds its byte limit"
                        )
                    content = _read_limited(file_fd, max_bytes)
                finally:
                    os.close(file_fd)
            finally:
                os.close(parent_fd)
        try:
            decoded = content.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ToolExecutorFailure(
                "FILE_ENCODING_INVALID", "workspace file is not UTF-8"
            ) from exc
        return {
            "path": "/".join(parts),
            "content": decoded,
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    def _write_bytes(
        self,
        tenant_id: UUID,
        run_id: UUID,
        relative_path: str,
        content: bytes,
        overwrite: bool,
        max_total_bytes: int,
    ) -> dict[str, Any]:
        parts = _path_parts(relative_path)
        with self._workspace(tenant_id, run_id, exclusive=True) as workspace_fd:
            parent_fd = self._open_parent(workspace_fd, parts[:-1], create=True)
            temporary_name = f".nico-{uuid4().hex}.tmp"
            temporary_fd: int | None = None
            try:
                existing_size = self._existing_size(parent_fd, parts[-1], overwrite=overwrite)
                total = self._total_bytes(workspace_fd)
                if total - existing_size + len(content) > max_total_bytes:
                    raise ToolExecutorFailure(
                        "WORKSPACE_QUOTA_EXCEEDED",
                        "workspace content exceeds its total byte limit",
                    )
                temporary_fd = os.open(
                    temporary_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=parent_fd,
                )
                _write_all(temporary_fd, content)
                os.fsync(temporary_fd)
                os.close(temporary_fd)
                temporary_fd = None
                os.replace(
                    temporary_name,
                    parts[-1],
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                os.fsync(parent_fd)
            except ToolExecutorFailure:
                raise
            except OSError as exc:
                raise ToolExecutorFailure(
                    "FILE_WRITE_FAILED", "workspace file write failed"
                ) from exc
            finally:
                if temporary_fd is not None:
                    os.close(temporary_fd)
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary_name, dir_fd=parent_fd)
                os.close(parent_fd)
        return {
            "path": "/".join(parts),
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    @contextmanager
    def _workspace(self, tenant_id: UUID, run_id: UUID, *, exclusive: bool) -> Iterator[int]:
        process_lock = self._process_lock(tenant_id, run_id)
        process_lock.acquire()
        root_fd: int | None = None
        tenant_fd: int | None = None
        workspace_fd: int | None = None
        lock_fd: int | None = None
        try:
            root_fd = os.open(self.root, _DIRECTORY_FLAGS)
            tenant_fd = _mkdir_and_open(root_fd, str(tenant_id))
            workspace_fd = _mkdir_and_open(tenant_fd, str(run_id))
            lock_fd = os.open(
                _LOCK_NAME,
                os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=workspace_fd,
            )
            fcntl.flock(lock_fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield workspace_fd
        except ToolExecutorFailure:
            raise
        except OSError as exc:
            raise ToolExecutorFailure(
                "WORKSPACE_UNAVAILABLE", "Run workspace is unavailable"
            ) from exc
        finally:
            if lock_fd is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            if workspace_fd is not None:
                os.close(workspace_fd)
            if tenant_fd is not None:
                os.close(tenant_fd)
            if root_fd is not None:
                os.close(root_fd)
            process_lock.release()

    @staticmethod
    def _open_parent(workspace_fd: int, parts: tuple[str, ...], *, create: bool) -> int:
        current_fd = os.dup(workspace_fd)
        try:
            for part in parts:
                if create:
                    with contextlib.suppress(FileExistsError):
                        os.mkdir(part, mode=0o700, dir_fd=current_fd)
                next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=current_fd)
                os.close(current_fd)
                current_fd = next_fd
            return current_fd
        except FileNotFoundError as exc:
            os.close(current_fd)
            raise ToolExecutorFailure(
                "FILE_NOT_FOUND", "workspace directory was not found"
            ) from exc
        except OSError as exc:
            os.close(current_fd)
            raise ToolExecutorFailure(
                "FILE_PATH_DENIED", "workspace directory is not safe"
            ) from exc

    @staticmethod
    def _existing_size(parent_fd: int, name: str, *, overwrite: bool) -> int:
        try:
            file_fd = os.open(name, _READ_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError:
            return 0
        except OSError as exc:
            raise ToolExecutorFailure("FILE_PATH_DENIED", "workspace target is not safe") from exc
        try:
            metadata = os.fstat(file_fd)
            _require_private_regular_file(metadata)
            if not overwrite:
                raise ToolExecutorFailure("FILE_EXISTS", "workspace file already exists")
            return metadata.st_size
        finally:
            os.close(file_fd)

    def _total_bytes(self, directory_fd: int) -> int:
        total = 0
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                if entry.name == _LOCK_NAME:
                    continue
                metadata = entry.stat(follow_symlinks=False)
                if stat.S_ISREG(metadata.st_mode):
                    total += metadata.st_size
                elif stat.S_ISDIR(metadata.st_mode):
                    child_fd = os.open(entry.name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
                    try:
                        total += self._total_bytes(child_fd)
                    finally:
                        os.close(child_fd)
        return total

    @staticmethod
    def _effective_limit(value: Any, platform_limit: int) -> int:
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return min(value, platform_limit)
        return platform_limit

    def _process_lock(self, tenant_id: UUID, run_id: UUID) -> threading.Lock:
        key = (tenant_id, run_id)
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())


class FileReadExecutor:
    spec = ToolDefinitionSpec(
        name="file.read",
        version="1.0.0",
        description="Read one UTF-8 text file from the tenant and Run scoped workspace",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string", "minLength": 1, "maxLength": 500}},
            "required": ["path"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "bytes": {"type": "integer", "minimum": 0},
                "sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            },
            "required": ["path", "content", "bytes", "sha256"],
            "additionalProperties": False,
        },
        permission="filesystem.read",
        timeout_seconds=10,
        retry_policy=ToolRetryPolicy(),
        isolation=ToolIsolation.WORKSPACE,
        risk=ToolRisk.LOW,
        max_output_bytes=1_100_000,
    )
    implementation_hash = canonical_hash({"executor": "file.read", "revision": 1})

    def __init__(self, workspace: WorkspaceManager) -> None:
        self.workspace = workspace

    async def execute(self, context, arguments, secrets):
        return ToolExecutionResult(
            output=await self.workspace.read_text(context, arguments["path"])
        )


class FileWriteExecutor:
    spec = ToolDefinitionSpec(
        name="file.write",
        version="1.0.0",
        description="Atomically write one UTF-8 text file inside the scoped Run workspace",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": 500},
                "content": {"type": "string"},
                "overwrite": {"type": "boolean", "default": False},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "bytes": {"type": "integer", "minimum": 0},
                "sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            },
            "required": ["path", "bytes", "sha256"],
            "additionalProperties": False,
        },
        permission="filesystem.write",
        timeout_seconds=10,
        retry_policy=ToolRetryPolicy(),
        isolation=ToolIsolation.WORKSPACE,
        risk=ToolRisk.MEDIUM,
        max_output_bytes=2048,
    )
    implementation_hash = canonical_hash({"executor": "file.write", "revision": 1})

    def __init__(self, workspace: WorkspaceManager) -> None:
        self.workspace = workspace

    async def execute(self, context, arguments, secrets):
        return ToolExecutionResult(
            output=await self.workspace.write_text(
                context,
                arguments["path"],
                arguments["content"],
                overwrite=bool(arguments.get("overwrite", False)),
            )
        )


class ReportWriteExecutor:
    spec = ToolDefinitionSpec(
        name="report.write",
        version="1.0.0",
        description="Write a Markdown or canonical JSON report in the scoped Run workspace",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": 500},
                "format": {"enum": ["markdown", "json"]},
                "content": {},
                "overwrite": {"type": "boolean", "default": False},
            },
            "required": ["path", "format", "content"],
            "additionalProperties": False,
        },
        output_schema=FileWriteExecutor.spec.output_schema,
        permission="report.write",
        timeout_seconds=10,
        retry_policy=ToolRetryPolicy(),
        isolation=ToolIsolation.WORKSPACE,
        risk=ToolRisk.MEDIUM,
        max_output_bytes=2048,
    )
    implementation_hash = canonical_hash({"executor": "report.write", "revision": 1})

    def __init__(self, workspace: WorkspaceManager) -> None:
        self.workspace = workspace

    async def execute(self, context, arguments, secrets):
        relative_path = arguments["path"]
        report_format = arguments["format"]
        if report_format == "markdown":
            if not relative_path.endswith(".md") or not isinstance(arguments["content"], str):
                raise ToolExecutorFailure(
                    "REPORT_FORMAT_INVALID", "Markdown reports require a .md path and text content"
                )
            content = arguments["content"]
        else:
            if not relative_path.endswith(".json"):
                raise ToolExecutorFailure(
                    "REPORT_FORMAT_INVALID", "JSON reports require a .json path"
                )
            try:
                content = canonical_json(arguments["content"]) + "\n"
            except (TypeError, ValueError) as exc:
                raise ToolExecutorFailure(
                    "REPORT_FORMAT_INVALID", "JSON report content must be finite JSON data"
                ) from exc
        return ToolExecutionResult(
            output=await self.workspace.write_text(
                context,
                relative_path,
                content,
                overwrite=bool(arguments.get("overwrite", False)),
            )
        )


def _path_parts(relative_path: str) -> tuple[str, ...]:
    if (
        not isinstance(relative_path, str)
        or not relative_path
        or len(relative_path) > _MAX_RELATIVE_PATH
        or "\x00" in relative_path
        or "\\" in relative_path
        or relative_path.startswith("/")
    ):
        raise ToolExecutorFailure("FILE_PATH_INVALID", "workspace path must be relative")
    parts = tuple(relative_path.split("/"))
    if any(
        not part
        or part in {".", ".."}
        or len(part) > _MAX_SEGMENT
        or part == _LOCK_NAME
        or part.startswith(".nico-")
        for part in parts
    ):
        raise ToolExecutorFailure("FILE_PATH_INVALID", "workspace path contains a denied segment")
    return parts


def _mkdir_and_open(parent_fd: int, name: str) -> int:
    with contextlib.suppress(FileExistsError):
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)


def _require_private_regular_file(metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ToolExecutorFailure(
            "FILE_TYPE_DENIED", "workspace path is not a private regular file"
        )


def _read_limited(file_fd: int, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    remaining = max_bytes + 1
    while remaining > 0:
        chunk = os.read(file_fd, min(65_536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    content = b"".join(chunks)
    if len(content) > max_bytes:
        raise ToolExecutorFailure("FILE_TOO_LARGE", "workspace file exceeds its byte limit")
    return content


def _write_all(file_fd: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(file_fd, view)
        if written <= 0:
            raise OSError("short workspace write")
        view = view[written:]
