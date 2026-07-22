"""Slash command parsing and discoverable command metadata."""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from nico_agent.cli.errors import CliError


@dataclass(frozen=True, slots=True)
class SlashCommand:
    name: str
    args: tuple[str, ...]
    raw_args: str


COMMANDS: dict[str, tuple[str, str]] = {
    "help": ("会话", "显示命令帮助"),
    "new": ("会话", "创建并切换到新会话"),
    "continue": ("会话", "切换到最近活跃会话"),
    "resume": ("会话", "切换到指定会话：/resume ID"),
    "history": ("会话", "查看当前会话历史"),
    "conversations": ("会话", "列出活跃会话"),
    "title": ("会话", "修改标题：/title TEXT"),
    "compact": ("会话", "压缩已完成历史并保留可审计摘要"),
    "attach": ("会话", "暂存本地文件到下一轮：/attach PATH"),
    "download": ("会话", "下载产物：/download ARTIFACT_ID [PATH]"),
    "exit": ("会话", "退出交互模式"),
    "status": ("状态", "查看当前会话和最后一次运行"),
    "permissions": ("状态", "查看或设置会话权限：/permissions [ask|auto-medium|auto-all]"),
    "queue": ("状态", "查看或控制队列：/queue [resume|cancel TURN]"),
    "agent": ("状态", "查看当前 Agent"),
    "version": ("状态", "查看冻结的 Agent 版本"),
    "runtime": ("状态", "查看 Runtime 会话"),
    "usage": ("状态", "查看 Token 与费用用量"),
    "context": ("状态", "查看上下文快照"),
    "plan": ("检查", "查看运行计划"),
    "steps": ("检查", "查看运行步骤"),
    "tools": ("检查", "查看工具调用"),
    "approvals": ("检查", "查看当前运行的工具审批"),
    "children": ("检查", "查看子运行"),
    "messages": ("检查", "查看 Agent 消息"),
    "artifacts": ("检查", "查看产物清单"),
    "audit": ("检查", "查看相关审计记录"),
    "inspect": ("检查", "汇总检查当前运行"),
    "guide": ("项目", "指导当前项目 Run：/guide TEXT"),
    "escalate": ("项目", "把项目范围变更交给 Lead：/escalate TEXT"),
    "interventions": ("项目", "查看当前 Run 的指导状态"),
    "withdraw": ("项目", "撤回待消费指导：/withdraw ID [REASON]"),
    "cancel": ("控制", "取消当前活动或队首轮次"),
    "retry": ("控制", "重试导致队列暂停的轮次"),
    "approve": ("控制", "批准工具：/approve ID once|run"),
    "reject": ("控制", "拒绝工具：/reject ID [REASON]"),
}


def parse_slash(value: str) -> SlashCommand | None:
    stripped = value.strip()
    if not stripped.startswith("/"):
        return None
    command_text = stripped[1:]
    try:
        parts = shlex.split(command_text)
    except ValueError as exc:
        raise CliError("INVALID_SLASH_COMMAND", str(exc), exit_code=2) from exc
    if not parts:
        return SlashCommand("help", (), "")
    name = parts[0].lower()
    if name == "quit":
        name = "exit"
    if name not in COMMANDS:
        raise CliError(
            "UNKNOWN_SLASH_COMMAND",
            f"unknown command '/{name}'; use /help",
            exit_code=2,
        )
    raw_args = command_text[len(parts[0]) :].strip()
    return SlashCommand(name, tuple(parts[1:]), raw_args)


def help_rows() -> list[dict[str, str]]:
    return [
        {"group": group, "command": f"/{name}", "description": description}
        for name, (group, description) in COMMANDS.items()
    ]
