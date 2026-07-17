from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest

from nico_agent.tools import ToolExecutionContext
from nico_agent.tools.builtin import (
    FileReadExecutor,
    FileWriteExecutor,
    ReportWriteExecutor,
    WorkspaceManager,
)
from nico_agent.tools.errors import ToolExecutorFailure


def _context(*, tenant_id=None, run_id=None, config=None) -> ToolExecutionContext:
    return ToolExecutionContext(
        tenant_id=tenant_id or uuid4(),
        run_id=run_id or uuid4(),
        run_step_id=uuid4(),
        actor_id="workspace-test",
        correlation_id=uuid4(),
        tool_config=config or {},
    )


@pytest.mark.asyncio
async def test_file_write_read_overwrite_and_relative_output(tmp_path: Path) -> None:
    workspace = WorkspaceManager(tmp_path / "workspaces", max_file_bytes=1024)
    writer = FileWriteExecutor(workspace)
    reader = FileReadExecutor(workspace)
    context = _context()

    written = await writer.execute(
        context,
        {"path": "notes/result.txt", "content": "hello 世界"},
        {},
    )
    read = await reader.execute(context, {"path": "notes/result.txt"}, {})

    assert written.output["path"] == "notes/result.txt"
    assert str(tmp_path) not in str(written.output)
    assert read.output["content"] == "hello 世界"
    assert read.output["sha256"] == written.output["sha256"]

    with pytest.raises(ToolExecutorFailure) as existing:
        await writer.execute(
            context,
            {"path": "notes/result.txt", "content": "changed"},
            {},
        )
    assert existing.value.code == "FILE_EXISTS"

    replaced = await writer.execute(
        context,
        {"path": "notes/result.txt", "content": "changed", "overwrite": True},
        {},
    )
    assert replaced.output["sha256"] != written.output["sha256"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "../outside",
        "safe/../../outside",
        "safe//file",
        "safe/./file",
        "safe\\file",
        "safe/\x00file",
        ".nico-workspace.lock",
        ".nico-user.tmp",
    ],
)
async def test_paths_fail_closed_before_filesystem_access(tmp_path: Path, path: str) -> None:
    writer = FileWriteExecutor(WorkspaceManager(tmp_path / "workspaces"))

    with pytest.raises(ToolExecutorFailure) as captured:
        await writer.execute(_context(), {"path": path, "content": "denied"}, {})

    assert captured.value.code == "FILE_PATH_INVALID"
    assert str(tmp_path) not in captured.value.message


@pytest.mark.asyncio
async def test_symlink_hardlink_and_fifo_cannot_escape_or_block(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    workspace = WorkspaceManager(root)
    writer = FileWriteExecutor(workspace)
    reader = FileReadExecutor(workspace)
    context = _context()
    await writer.execute(context, {"path": "seed.txt", "content": "seed"}, {})
    run_root = root / str(context.tenant_id) / str(context.run_id)
    external_file = tmp_path / "external-secret.txt"
    external_file.write_text("outside secret", encoding="utf-8")
    external_directory = tmp_path / "external-directory"
    external_directory.mkdir()
    (external_directory / "inside.txt").write_text("outside", encoding="utf-8")
    (run_root / "symlink.txt").symlink_to(external_file)
    (run_root / "linked-directory").symlink_to(external_directory, target_is_directory=True)
    os.link(external_file, run_root / "hardlink.txt")
    os.mkfifo(run_root / "pipe")

    expectations = {
        "symlink.txt": "FILE_PATH_DENIED",
        "linked-directory/inside.txt": "FILE_PATH_DENIED",
        "hardlink.txt": "FILE_TYPE_DENIED",
        "pipe": "FILE_TYPE_DENIED",
    }
    for path, code in expectations.items():
        with pytest.raises(ToolExecutorFailure) as captured:
            await reader.execute(context, {"path": path}, {})
        assert captured.value.code == code


@pytest.mark.asyncio
async def test_workspace_isolation_and_policy_limits_are_restrictive(tmp_path: Path) -> None:
    workspace = WorkspaceManager(
        tmp_path / "workspaces",
        max_file_bytes=20,
        max_total_bytes=25,
    )
    writer = FileWriteExecutor(workspace)
    reader = FileReadExecutor(workspace)
    first = _context()
    other_run = _context(tenant_id=first.tenant_id)
    other_tenant = _context(run_id=first.run_id)
    await writer.execute(first, {"path": "private.txt", "content": "1234567890"}, {})

    for hidden_context in (other_run, other_tenant):
        with pytest.raises(ToolExecutorFailure) as hidden:
            await reader.execute(hidden_context, {"path": "private.txt"}, {})
        assert hidden.value.code == "FILE_NOT_FOUND"

    restricted = first.model_copy(update={"tool_config": {"max_file_bytes": 5}})
    with pytest.raises(ToolExecutorFailure) as limited:
        await reader.execute(restricted, {"path": "private.txt"}, {})
    assert limited.value.code == "FILE_TOO_LARGE"

    with pytest.raises(ToolExecutorFailure) as total:
        await writer.execute(first, {"path": "second.txt", "content": "x" * 16}, {})
    assert total.value.code == "WORKSPACE_QUOTA_EXCEEDED"


@pytest.mark.asyncio
async def test_workspace_lock_keeps_concurrent_writes_within_total_quota(tmp_path: Path) -> None:
    workspace = WorkspaceManager(
        tmp_path / "workspaces",
        max_file_bytes=10,
        max_total_bytes=15,
    )
    writer = FileWriteExecutor(workspace)
    context = _context()

    results = await asyncio.gather(
        writer.execute(context, {"path": "one.txt", "content": "1" * 10}, {}),
        writer.execute(context, {"path": "two.txt", "content": "2" * 10}, {}),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, Exception) for result in results) == 1
    errors = [result for result in results if isinstance(result, ToolExecutorFailure)]
    assert len(errors) == 1 and errors[0].code == "WORKSPACE_QUOTA_EXCEEDED"


@pytest.mark.asyncio
async def test_report_writer_enforces_format_and_uses_canonical_json(tmp_path: Path) -> None:
    workspace = WorkspaceManager(tmp_path / "workspaces")
    reports = ReportWriteExecutor(workspace)
    reader = FileReadExecutor(workspace)
    context = _context()

    await reports.execute(
        context,
        {"path": "reports/summary.md", "format": "markdown", "content": "# Summary\n"},
        {},
    )
    await reports.execute(
        context,
        {
            "path": "reports/data.json",
            "format": "json",
            "content": {"z": 1, "a": [2, 3]},
        },
        {},
    )
    markdown = await reader.execute(context, {"path": "reports/summary.md"}, {})
    json_report = await reader.execute(context, {"path": "reports/data.json"}, {})
    assert markdown.output["content"] == "# Summary\n"
    assert json_report.output["content"] == '{"a":[2,3],"z":1}\n'

    with pytest.raises(ToolExecutorFailure) as invalid:
        await reports.execute(
            context,
            {"path": "reports/wrong.txt", "format": "markdown", "content": "text"},
            {},
        )
    assert invalid.value.code == "REPORT_FORMAT_INVALID"
