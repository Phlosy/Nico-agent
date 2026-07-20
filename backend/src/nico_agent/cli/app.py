"""Nico's first-class, remote-only CLI entry point."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar
from uuid import UUID

import typer

from nico_agent import __version__
from nico_agent.cli.chat import ChatRunner
from nico_agent.cli.client import NicoApiClient
from nico_agent.cli.config import CliConfig, ConfigStore, Profile, validate_profile_name
from nico_agent.cli.errors import CliError
from nico_agent.cli.execution import ExecRunner, RunWatcher, load_exec_input, write_result
from nico_agent.cli.output import Output

T = TypeVar("T")

app = typer.Typer(
    name="nico",
    help="Nico Agent 的远程命令行客户端。",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
config_app = typer.Typer(help="管理本地连接 profile。", no_args_is_help=True)
project_app = typer.Typer(help="查询 Project。", no_args_is_help=True)
agent_app = typer.Typer(help="查询 Agent 与不可变版本。", no_args_is_help=True)
task_app = typer.Typer(help="查询 Task。", no_args_is_help=True)
run_app = typer.Typer(help="查询 Run、Runtime 和持久化事件。", no_args_is_help=True)
conversation_app = typer.Typer(help="查询持久化 Conversation。", no_args_is_help=True)

app.add_typer(config_app, name="config")
app.add_typer(project_app, name="project")
app.add_typer(agent_app, name="agent")
app.add_typer(task_app, name="task")
app.add_typer(run_app, name="run")
app.add_typer(conversation_app, name="conversation")


@dataclass(slots=True)
class AppState:
    config_file: Path | None
    profile_name: str | None
    base_url: str | None
    tenant_id: UUID | None
    actor_id: str | None
    json_mode: bool
    no_color: bool

    def store(self) -> ConfigStore:
        return ConfigStore(self.config_file)

    def output(self) -> Output:
        return Output(json_mode=self.json_mode, no_color=self.no_color)

    def resolved_profile(self):
        return self.store().resolve(
            profile_name=self.profile_name,
            base_url=self.base_url,
            tenant_id=self.tenant_id,
            actor_id=self.actor_id,
        )


@app.callback()
def main(
    ctx: typer.Context,
    profile: str | None = typer.Option(None, "--profile", "-p", help="使用指定 profile。"),
    config_file: Path | None = typer.Option(
        None,
        "--config-file",
        envvar="NICO_CONFIG_FILE",
        help="覆盖配置文件路径。",
    ),
    api_url: str | None = typer.Option(None, "--api-url", envvar="NICO_API_URL"),
    tenant_id: UUID | None = typer.Option(None, "--tenant-id", envvar="NICO_TENANT_ID"),
    actor_id: str | None = typer.Option(None, "--actor-id", envvar="NICO_ACTOR_ID"),
    json_output: bool = typer.Option(False, "--json", help="输出机器可读 JSON。"),
    no_color: bool = typer.Option(False, "--no-color", help="禁用 ANSI 色彩。"),
    version: bool = typer.Option(
        False,
        "--version",
        is_eager=True,
        callback=lambda value: _show_version_and_exit(value),
        help="显示客户端版本并退出。",
    ),
) -> None:
    del version
    ctx.obj = AppState(
        config_file=config_file,
        profile_name=profile,
        base_url=api_url,
        tenant_id=tenant_id,
        actor_id=actor_id,
        json_mode=json_output,
        no_color=no_color or "NO_COLOR" in os.environ,
    )


def _show_version_and_exit(value: bool) -> bool:
    if value:
        typer.echo(f"nico {__version__}")
        raise typer.Exit()
    return value


def _state(ctx: typer.Context) -> AppState:
    state = ctx.find_root().obj
    if not isinstance(state, AppState):
        raise RuntimeError("CLI context was not initialized")
    return state


def _guard(state: AppState, operation: Callable[[], T]) -> T:
    try:
        return operation()
    except CliError as exc:
        state.output().error(exc)
        raise typer.Exit(exc.exit_code) from exc


def _api(state: AppState, operation: Callable[[NicoApiClient], T]) -> T:
    def invoke() -> T:
        with NicoApiClient(state.resolved_profile()) as client:
            return operation(client)

    return _guard(state, invoke)


@app.command("chat")
def chat_command(
    ctx: typer.Context,
    message: str | None = typer.Argument(None, help="单轮消息；省略时进入交互模式。"),
    project_id: UUID | None = typer.Option(None, "--project"),
    agent_id: UUID | None = typer.Option(None, "--agent"),
    agent_version_id: UUID | None = typer.Option(None, "--version"),
    resume_id: UUID | None = typer.Option(None, "--resume"),
    continue_latest: bool = typer.Option(False, "--continue"),
    read_only: bool = typer.Option(False, "--read-only"),
    title: str = typer.Option("New conversation", "--title"),
) -> None:
    state = _state(ctx)

    def operation() -> None:
        if state.json_mode and message is None and not read_only:
            raise CliError(
                "JSON_INTERACTIVE_UNSUPPORTED",
                "--json chat requires MESSAGE or --read-only",
                exit_code=2,
            )
        with NicoApiClient(state.resolved_profile()) as client:
            runner = ChatRunner(
                client,
                state.output(),
                history_path=state.store().path.with_name("history"),
            )
            conversation = runner.resolve(
                project_id=str(project_id) if project_id else None,
                agent_id=str(agent_id) if agent_id else None,
                agent_version_id=str(agent_version_id) if agent_version_id else None,
                resume_id=str(resume_id) if resume_id else None,
                continue_latest=continue_latest,
                title=title,
            )
            if read_only:
                history = runner.history(conversation["id"])
                if state.json_mode:
                    state.output().emit({"conversation": conversation, "turns": history})
                else:
                    runner.run_interactive(conversation, read_only=True)
                return
            if message is not None:
                result = runner.submit(conversation, message)
                if state.json_mode:
                    state.output().emit(result)
                return
            runner.run_interactive(conversation, read_only=False)

    _guard(state, operation)


@app.command("exec")
def exec_command(
    ctx: typer.Context,
    prompt: str | None = typer.Argument(None, help="要执行的任务说明。"),
    project_id: UUID | None = typer.Option(None, "--project"),
    agent_id: UUID | None = typer.Option(None, "--agent"),
    agent_version_id: UUID | None = typer.Option(None, "--version"),
    input_file: Path | None = typer.Option(None, "--input", exists=True, dir_okay=False),
    output_file: Path | None = typer.Option(None, "--output", dir_okay=False),
    json_output: bool = typer.Option(False, "--json", help="输出机器可读 JSON。"),
    detach: bool = typer.Option(False, "--detach", help="提交后立即返回 Run 标识。"),
    title: str | None = typer.Option(None, "--title"),
) -> None:
    """提交一次可审计执行；默认持续监看，--detach 立即返回。"""

    state = _state(ctx)
    if json_output:
        state.json_mode = True

    def operation() -> dict[str, Any]:
        values: dict[str, Any] = load_exec_input(input_file) if input_file else {}
        if prompt is not None and values.get("prompt") is not None:
            raise CliError(
                "EXEC_PROMPT_CONFLICT",
                "provide either PROMPT or --input prompt, not both",
                exit_code=2,
            )
        resolved_prompt = prompt if prompt is not None else values.get("prompt")
        resolved_project = str(project_id) if project_id else values.get("project_id")
        resolved_agent = str(agent_id) if agent_id else values.get("agent_id")
        resolved_version = (
            str(agent_version_id) if agent_version_id else values.get("agent_version_id")
        )
        if not isinstance(resolved_prompt, str) or not resolved_prompt.strip():
            raise CliError(
                "EXEC_PROMPT_REQUIRED",
                "exec requires PROMPT or a prompt field in --input",
                exit_code=2,
            )
        if not resolved_project or not resolved_agent:
            raise CliError(
                "EXEC_TARGET_REQUIRED",
                "exec requires --project and --agent (or matching --input fields)",
                exit_code=2,
            )
        resolved_title = title or values.get("title") or f"Exec: {resolved_prompt[:80]}"
        with NicoApiClient(state.resolved_profile()) as client:
            result = ExecRunner(client, state.output()).execute(
                prompt=resolved_prompt,
                project_id=str(resolved_project),
                agent_id=str(resolved_agent),
                agent_version_id=str(resolved_version) if resolved_version else None,
                title=str(resolved_title),
                detach=detach,
            )
        if output_file is not None:
            write_result(output_file, result)
        return result

    result = _guard(state, operation)
    if state.json_mode:
        state.output().emit(result)


@run_app.command("watch")
def run_watch(
    ctx: typer.Context,
    run_id: UUID,
    after_sequence: int = typer.Option(0, "--after", min=0),
    json_output: bool = typer.Option(False, "--json", help="结束后输出一个 JSON 文档。"),
) -> None:
    """从持久化游标持续监看 Run；Ctrl+C 只退出监看。"""

    state = _state(ctx)
    if json_output:
        state.json_mode = True

    def operation() -> dict[str, Any]:
        with NicoApiClient(state.resolved_profile()) as client:
            return RunWatcher(client, state.output()).watch(
                str(run_id),
                after_sequence=after_sequence,
            )

    result = _guard(state, operation)
    if state.json_mode:
        state.output().emit(result)
    else:
        state.output().emit(result["run"], title="Run")


@app.command("version")
def version_command(
    ctx: typer.Context,
    server: bool = typer.Option(False, "--server", help="同时查询服务端版本。"),
) -> None:
    state = _state(ctx)
    value: dict[str, Any] = {"client": __version__}
    if server:
        live = _api(state, lambda client: client.liveness())
        value["server"] = live.get("version")
        value["service"] = live.get("service")
    state.output().emit(value, title="Nico Version")


@app.command("health")
def health_command(
    ctx: typer.Context,
    live: bool = typer.Option(False, "--live", help="只检查进程存活。"),
) -> None:
    state = _state(ctx)
    result = _api(
        state,
        (lambda client: client.liveness()) if live else (lambda client: client.readiness()),
    )
    state.output().emit(result, title="Nico Health")


@app.command("doctor")
def doctor_command(ctx: typer.Context) -> None:
    state = _state(ctx)
    output = state.output()
    checks: list[dict[str, Any]] = []
    failed = False
    try:
        profile = state.resolved_profile()
        checks.append({"check": "config", "status": "pass", "detail": profile.name})
        permissions = state.store().permissions()
        checks.append(
            {
                "check": "config_permissions",
                "status": "pass" if permissions["secure"] else "fail",
                "detail": permissions["mode"] or "not created",
            }
        )
        failed = failed or not permissions["secure"]
        checks.append(
            {
                "check": "tenant_context",
                "status": "pass" if profile.tenant_id else "warning",
                "detail": str(profile.tenant_id) if profile.tenant_id else "not configured",
            }
        )
        with NicoApiClient(profile) as client:
            for name, probe in (("api_live", client.liveness), ("dependencies", client.readiness)):
                try:
                    result = probe()
                    status = result.get("status", "unknown")
                    good = status in {"alive", "ready"}
                    checks.append(
                        {"check": name, "status": "pass" if good else "fail", "detail": status}
                    )
                    failed = failed or not good
                except CliError as exc:
                    checks.append({"check": name, "status": "fail", "detail": exc.code})
                    failed = True
    except CliError as exc:
        checks.append({"check": "config", "status": "fail", "detail": exc.code})
        failed = True
    output.table(checks, title="Nico Doctor", columns=["check", "status", "detail"])
    if failed:
        raise typer.Exit(1)


@config_app.command("path")
def config_path(ctx: typer.Context) -> None:
    state = _state(ctx)
    state.output().emit({"path": str(state.store().path), **state.store().permissions()})


@config_app.command("list")
def config_list(ctx: typer.Context) -> None:
    state = _state(ctx)

    def operation() -> tuple[CliConfig, list[dict[str, Any]]]:
        config = state.store().load()
        rows = []
        for name, profile in sorted(config.profiles.items()):
            rows.append(
                {
                    "name": name,
                    "current": name == config.current_profile,
                    "base_url": profile.base_url,
                    "tenant_id": str(profile.tenant_id) if profile.tenant_id else None,
                }
            )
        return config, rows

    _config, rows = _guard(state, operation)
    state.output().table(rows, title="Nico Profiles")


@config_app.command("show")
def config_show(ctx: typer.Context, name: str | None = typer.Argument(None)) -> None:
    state = _state(ctx)
    resolved = _guard(
        state,
        lambda: state.store().resolve(
            profile_name=name or state.profile_name,
            base_url=state.base_url,
            tenant_id=state.tenant_id,
            actor_id=state.actor_id,
        ),
    )
    state.output().emit(resolved.public_dict(), title="Nico Profile")


@config_app.command("set")
def config_set(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Profile 名称。"),
    base_url: str | None = typer.Option(None, "--api-url"),
    tenant_id: UUID | None = typer.Option(None, "--tenant-id"),
    clear_tenant: bool = typer.Option(False, "--clear-tenant"),
    actor_id: str | None = typer.Option(None, "--actor-id"),
    api_token_env: str | None = typer.Option(
        None, "--api-token-env", help="保存令牌所在的环境变量名，不保存令牌。"
    ),
    clear_token_env: bool = typer.Option(False, "--clear-token-env"),
    timeout_seconds: float | None = typer.Option(None, "--timeout"),
    verify_tls: bool | None = typer.Option(None, "--verify-tls/--no-verify-tls"),
) -> None:
    state = _state(ctx)

    def operation() -> Profile:
        validate_profile_name(name)
        store = state.store()
        config = store.load()
        current = config.profiles.get(name, Profile())
        values = current.model_dump()
        if base_url is not None:
            values["base_url"] = base_url
        if tenant_id is not None or clear_tenant:
            values["tenant_id"] = None if clear_tenant else tenant_id
        if actor_id is not None:
            values["actor_id"] = actor_id
        if api_token_env is not None or clear_token_env:
            values["api_token_env"] = None if clear_token_env else api_token_env
        if timeout_seconds is not None:
            values["timeout_seconds"] = timeout_seconds
        if verify_tls is not None:
            values["verify_tls"] = verify_tls
        try:
            profile = Profile.model_validate(values)
        except ValueError as exc:
            raise CliError("INVALID_PROFILE", str(exc), exit_code=2) from exc
        profiles = dict(config.profiles)
        profiles[name] = profile
        store.save(CliConfig(current_profile=config.current_profile, profiles=profiles))
        return profile

    profile = _guard(state, operation)
    state.output().emit(
        {
            "name": name,
            "base_url": profile.base_url,
            "tenant_id": str(profile.tenant_id) if profile.tenant_id else None,
            "actor_id": profile.actor_id,
            "api_token_env": profile.api_token_env,
            "timeout_seconds": profile.timeout_seconds,
            "verify_tls": profile.verify_tls,
        },
        title="Profile Saved",
    )


@config_app.command("use")
def config_use(ctx: typer.Context, name: str) -> None:
    state = _state(ctx)

    def operation() -> None:
        validate_profile_name(name)
        store = state.store()
        config = store.load()
        if name not in config.profiles:
            raise CliError("PROFILE_NOT_FOUND", f"profile '{name}' does not exist", exit_code=2)
        store.save(CliConfig(current_profile=name, profiles=config.profiles))

    _guard(state, operation)
    state.output().emit({"current_profile": name})


@config_app.command("delete")
def config_delete(ctx: typer.Context, name: str) -> None:
    state = _state(ctx)

    def operation() -> None:
        validate_profile_name(name)
        store = state.store()
        config = store.load()
        if name not in config.profiles:
            raise CliError("PROFILE_NOT_FOUND", f"profile '{name}' does not exist", exit_code=2)
        if name == config.current_profile:
            raise CliError(
                "CURRENT_PROFILE_DELETE_DENIED",
                "switch to another profile before deleting the current profile",
                exit_code=2,
            )
        profiles = dict(config.profiles)
        del profiles[name]
        store.save(CliConfig(current_profile=config.current_profile, profiles=profiles))

    _guard(state, operation)
    state.output().emit({"deleted": name})


@project_app.command("list")
def project_list(ctx: typer.Context) -> None:
    state = _state(ctx)
    rows = _api(state, lambda client: client.list_projects())
    state.output().table(rows, title="Projects", columns=["id", "name", "status", "revision"])


@project_app.command("get")
def project_get(ctx: typer.Context, project_id: UUID) -> None:
    state = _state(ctx)
    state.output().emit(
        _api(state, lambda client: client.get_project(str(project_id))), title="Project"
    )


@agent_app.command("list")
def agent_list(ctx: typer.Context) -> None:
    state = _state(ctx)
    rows = _api(state, lambda client: client.list_agents())
    state.output().table(
        rows,
        title="Agents",
        columns=["id", "name", "display_name", "status", "current_version_id"],
    )


@agent_app.command("get")
def agent_get(ctx: typer.Context, agent_id: UUID) -> None:
    state = _state(ctx)
    state.output().emit(_api(state, lambda client: client.get_agent(str(agent_id))), title="Agent")


@agent_app.command("versions")
def agent_versions(ctx: typer.Context, agent_id: UUID) -> None:
    state = _state(ctx)
    rows = _api(state, lambda client: client.list_agent_versions(str(agent_id)))
    state.output().table(
        rows,
        title="Agent Versions",
        columns=["id", "version", "status", "runtime_provider", "execution_mode", "model_name"],
    )


@task_app.command("get")
def task_get(ctx: typer.Context, task_id: UUID) -> None:
    state = _state(ctx)
    state.output().emit(_api(state, lambda client: client.get_task(str(task_id))), title="Task")


@run_app.command("get")
def run_get(ctx: typer.Context, run_id: UUID) -> None:
    state = _state(ctx)
    state.output().emit(_api(state, lambda client: client.get_run(str(run_id))), title="Run")


@run_app.command("runtime")
def run_runtime(ctx: typer.Context, run_id: UUID) -> None:
    state = _state(ctx)
    state.output().emit(
        _api(state, lambda client: client.get_runtime(str(run_id))), title="Runtime"
    )


@run_app.command("events")
def run_events(ctx: typer.Context, run_id: UUID) -> None:
    state = _state(ctx)
    rows = _api(state, lambda client: client.list_run_events(str(run_id)))
    state.output().table(
        rows,
        title="Run Events",
        columns=["sequence", "event_type", "actor_id", "created_at"],
    )


@conversation_app.command("list")
def conversation_list(
    ctx: typer.Context,
    project_id: UUID | None = typer.Option(None, "--project"),
    agent_id: UUID | None = typer.Option(None, "--agent"),
    status: str | None = typer.Option(None, "--status"),
    limit: int = typer.Option(50, "--limit", min=1, max=100),
) -> None:
    state = _state(ctx)
    rows = _api(
        state,
        lambda client: client.list_conversations(
            project_id=str(project_id) if project_id else None,
            agent_id=str(agent_id) if agent_id else None,
            status=status,
            limit=limit,
        ),
    )
    state.output().table(
        rows,
        title="Conversations",
        columns=["id", "title", "status", "agent_id", "last_turn_id", "updated_at"],
    )


@conversation_app.command("get")
def conversation_get(ctx: typer.Context, conversation_id: UUID) -> None:
    state = _state(ctx)
    state.output().emit(
        _api(state, lambda client: client.get_conversation(str(conversation_id))),
        title="Conversation",
    )


@conversation_app.command("history")
def conversation_history(
    ctx: typer.Context,
    conversation_id: UUID,
    after_sequence: int = typer.Option(0, "--after", min=0),
    limit: int = typer.Option(100, "--limit", min=1, max=500),
) -> None:
    state = _state(ctx)
    rows = _api(
        state,
        lambda client: client.list_conversation_turns(
            str(conversation_id),
            after_sequence=after_sequence,
            limit=limit,
        ),
    )
    state.output().table(
        rows,
        title="Conversation History",
        columns=["sequence", "status", "user_input", "assistant_output", "run_id"],
    )


def run() -> None:
    app()
