"""Nico's first-class, remote-only CLI entry point."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar
from uuid import UUID

import typer

from nico_agent import __version__
from nico_agent.cli.capabilities import CapabilityCoordinator, CapabilityInput
from nico_agent.cli.chat import ChatRunner
from nico_agent.cli.client import NicoApiClient
from nico_agent.cli.config import CliConfig, ConfigStore, Profile, validate_profile_name
from nico_agent.cli.errors import CliError
from nico_agent.cli.execution import ExecRunner, RunWatcher, load_exec_input, write_result
from nico_agent.cli.output import Output
from nico_agent.cli.project import ProjectCli
from nico_agent.cli.provider import ProviderInput, ProviderOnboardingCoordinator
from nico_agent.cli.renderers import ProjectRenderer
from nico_agent.cli.service_bridge import ServiceBridge
from nico_agent.cli.setup import GuidedSetupCoordinator, GuidedSetupInput
from nico_agent.cli.web import WebConfigureInput, WebCoordinator

T = TypeVar("T")

app = typer.Typer(
    name="nico",
    help="Nico Agent 的远程命令行客户端。",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
config_app = typer.Typer(help="管理本地连接 profile。", no_args_is_help=True)
project_app = typer.Typer(help="创建、进入并管理 Project。", no_args_is_help=True)
agent_app = typer.Typer(help="查询 Agent 与不可变版本。", no_args_is_help=True)
task_app = typer.Typer(help="查询 Task。", no_args_is_help=True)
run_app = typer.Typer(help="查询 Run、Runtime 和持久化事件。", no_args_is_help=True)
conversation_app = typer.Typer(help="查询持久化 Conversation。", no_args_is_help=True)
provider_app = typer.Typer(help="交互式配置和验证 AI Provider。", no_args_is_help=True)
web_app = typer.Typer(help="配置、测试和关闭 Web 搜索能力。", no_args_is_help=True)

app.add_typer(config_app, name="config")
app.add_typer(project_app, name="project")
app.add_typer(agent_app, name="agent")
app.add_typer(task_app, name="task")
app.add_typer(run_app, name="run")
app.add_typer(conversation_app, name="conversation")
app.add_typer(provider_app, name="provider")
app.add_typer(web_app, name="web")


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
        build_version = os.environ.get("NICO_BUILD_VERSION")
        typer.echo(f"nico {build_version or __version__}")
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


def _provider_options(values: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        key, separator, option_value = value.partition("=")
        if not separator or not key:
            raise CliError(
                "PROVIDER_OPTION_INVALID",
                "Provider options must use KEY=VALUE",
                exit_code=2,
            )
        parsed[key] = option_value
    return parsed


def _run_provider_onboarding(
    state: AppState,
    *,
    provider_key: str | None,
    credential_ref: str | None,
    model: str | None,
    project_id: UUID | None,
    agent_id: UUID | None,
    expected_agent_revision: int | None,
    starter_agent_name: str | None,
    starter_agent_display_name: str | None,
    location: str | None,
    option: list[str],
    custom_provider_key: str | None,
    custom_provider_name: str | None,
    custom_protocol: str | None,
    custom_base_url: str | None,
    confirmed: bool,
) -> dict[str, Any]:
    profile = state.resolved_profile()
    with NicoApiClient(profile) as client:
        coordinator = ProviderOnboardingCoordinator(
            client,
            state.output(),
            service_bridge=ServiceBridge(profile),
            interactive=sys.stdin.isatty() and not state.json_mode,
            prompt=typer.prompt,
            confirm=typer.confirm,
        )
        return coordinator.onboard(
            ProviderInput(
                provider_key=provider_key,
                credential_ref=credential_ref,
                model=model,
                project_id=str(project_id) if project_id else None,
                agent_id=str(agent_id) if agent_id else None,
                expected_agent_revision=expected_agent_revision,
                starter_agent_name=starter_agent_name,
                starter_agent_display_name=starter_agent_display_name,
                location_key=location,
                provider_options=_provider_options(option),
                custom_provider_key=custom_provider_key,
                custom_provider_name=custom_provider_name,
                custom_protocol=custom_protocol,
                custom_base_url=custom_base_url,
                confirmed=confirmed,
            )
        )


@app.command("setup")
def setup_command(
    ctx: typer.Context,
    status_only: bool = typer.Option(
        False,
        "--status",
        help="仅显示四步设置状态，不启动或修改设置。",
    ),
    provider_key: str | None = typer.Option(None, "--provider"),
    credential_ref: str | None = typer.Option(None, "--credential-ref"),
    model: str | None = typer.Option(None, "--model"),
    project_id: UUID | None = typer.Option(None, "--project"),
    agent_id: UUID | None = typer.Option(None, "--agent"),
    expected_agent_revision: int | None = typer.Option(None, "--agent-revision", min=1),
    starter_agent_name: str | None = typer.Option(None, "--starter-name"),
    starter_agent_display_name: str | None = typer.Option(None, "--starter-display-name"),
    location: str | None = typer.Option(None, "--location"),
    option: list[str] | None = typer.Option(None, "--option", help="Provider KEY=VALUE option."),
    custom_provider_key: str | None = typer.Option(None, "--custom-key"),
    custom_provider_name: str | None = typer.Option(None, "--custom-name"),
    custom_protocol: str | None = typer.Option(None, "--protocol"),
    custom_base_url: str | None = typer.Option(None, "--base-url"),
    enable_web: bool = typer.Option(False, "--enable-web"),
    skip_web: bool = typer.Option(False, "--skip-web"),
    web_provider: str | None = typer.Option(None, "--web-provider"),
    web_endpoint: str | None = typer.Option(None, "--web-endpoint"),
    web_credential_ref: str | None = typer.Option(None, "--web-credential-ref"),
    web_dns: str | None = typer.Option(None, "--web-dns"),
    capability_profile: str | None = typer.Option(None, "--capability-profile"),
    capability_tool: list[str] | None = typer.Option(None, "--capability-tool"),
    capability_skill: list[UUID] | None = typer.Option(None, "--capability-skill"),
    all_capabilities: bool = typer.Option(False, "--all-capabilities"),
    clear_capabilities: bool = typer.Option(False, "--clear-capabilities"),
    accept_risk: list[str] | None = typer.Option(None, "--accept-risk"),
    approve_tools: bool = typer.Option(
        False,
        "--approve-tools",
        help="自动批准本次在线验证所需的 Tool 调用。",
    ),
    reconfigure: str | None = typer.Option(
        None,
        "--reconfigure",
        help="重新配置 model、web、capabilities 或 verification。",
    ),
    yes: bool = typer.Option(False, "--yes", help="确认发布服务端预览。"),
) -> None:
    """恢复并完成模型、Web、Agent 能力和在线验证四步设置。"""

    state = _state(ctx)

    if status_only:
        result = _api(state, lambda client: client.setup_readiness())
        state.output().emit(result, title="Nico Setup Status")
        return

    def operation() -> dict[str, Any]:
        resolved = state.resolved_profile()
        interactive = sys.stdin.isatty() and not state.json_mode
        with NicoApiClient(resolved) as client:
            web = WebCoordinator(
                client,
                state.output(),
                service_bridge=ServiceBridge(resolved),
                interactive=interactive,
                prompt=typer.prompt,
                confirm=typer.confirm,
            )
            capabilities = _capability_coordinator(state, client)
            coordinator = GuidedSetupCoordinator(
                client,
                state.output(),
                interactive=interactive,
                prompt=typer.prompt,
                confirm=typer.confirm,
                model_setup=lambda: _run_provider_onboarding(
                    state,
                    provider_key=provider_key,
                    credential_ref=credential_ref,
                    model=model,
                    project_id=project_id,
                    agent_id=agent_id,
                    expected_agent_revision=expected_agent_revision,
                    starter_agent_name=starter_agent_name,
                    starter_agent_display_name=starter_agent_display_name,
                    location=location,
                    option=option or [],
                    custom_provider_key=custom_provider_key,
                    custom_provider_name=custom_provider_name,
                    custom_protocol=custom_protocol,
                    custom_base_url=custom_base_url,
                    confirmed=yes,
                ),
                web_setup=lambda: web.configure(
                    WebConfigureInput(
                        provider=(
                            web_provider if web_provider is not None or interactive else "searxng"
                        ),
                        endpoint_key=web_endpoint,
                        credential_ref=web_credential_ref,
                        dns_resolver=web_dns,
                        confirmed=yes,
                        provider_only=True,
                    )
                ),
                capabilities=capabilities,
            )
            return coordinator.run(
                GuidedSetupInput(
                    skip_web=skip_web,
                    enable_web=enable_web,
                    automatically_approve_tools=approve_tools,
                    reconfigure=reconfigure,
                ),
                CapabilityInput(
                    profile=capability_profile,
                    tool_refs=capability_tool or [],
                    skill_version_ids=[str(value) for value in (capability_skill or [])],
                    select_all=all_capabilities,
                    clear_all=clear_capabilities,
                    accepted_risks=accept_risk or [],
                    confirmed=yes,
                ),
            )

    result = _guard(state, operation)
    state.output().emit(result, title="Nico Setup")


def _provider_add_command(
    ctx: typer.Context,
    provider_key: str | None,
    credential_ref: str | None,
    model: str | None,
    project_id: UUID | None,
    agent_id: UUID | None,
    expected_agent_revision: int | None,
    starter_agent_name: str | None,
    starter_agent_display_name: str | None,
    location: str | None,
    option: list[str] | None,
    custom_provider_key: str | None,
    custom_provider_name: str | None,
    custom_protocol: str | None,
    custom_base_url: str | None,
    yes: bool,
) -> None:
    state = _state(ctx)
    result = _guard(
        state,
        lambda: _run_provider_onboarding(
            state,
            provider_key=provider_key,
            credential_ref=credential_ref,
            model=model,
            project_id=project_id,
            agent_id=agent_id,
            expected_agent_revision=expected_agent_revision,
            starter_agent_name=starter_agent_name,
            starter_agent_display_name=starter_agent_display_name,
            location=location,
            option=option or [],
            custom_provider_key=custom_provider_key,
            custom_provider_name=custom_provider_name,
            custom_protocol=custom_protocol,
            custom_base_url=custom_base_url,
            confirmed=yes,
        ),
    )
    state.output().emit(result, title="Provider Ready")


@provider_app.command("add")
def provider_add(
    ctx: typer.Context,
    provider_key: str | None = typer.Argument(None),
    credential_ref: str | None = typer.Option(None, "--credential-ref"),
    model: str | None = typer.Option(None, "--model"),
    project_id: UUID | None = typer.Option(None, "--project"),
    agent_id: UUID | None = typer.Option(None, "--agent"),
    expected_agent_revision: int | None = typer.Option(None, "--agent-revision", min=1),
    starter_agent_name: str | None = typer.Option(None, "--starter-name"),
    starter_agent_display_name: str | None = typer.Option(None, "--starter-display-name"),
    location: str | None = typer.Option(None, "--location"),
    option: list[str] | None = typer.Option(None, "--option"),
    custom_provider_key: str | None = typer.Option(None, "--custom-key"),
    custom_provider_name: str | None = typer.Option(None, "--custom-name"),
    custom_protocol: str | None = typer.Option(None, "--protocol"),
    custom_base_url: str | None = typer.Option(None, "--base-url"),
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    """添加并验证 Provider，然后原子发布到一个 Agent。"""

    _provider_add_command(
        ctx,
        provider_key,
        credential_ref,
        model,
        project_id,
        agent_id,
        expected_agent_revision,
        starter_agent_name,
        starter_agent_display_name,
        location,
        option,
        custom_provider_key,
        custom_provider_name,
        custom_protocol,
        custom_base_url,
        yes,
    )


@provider_app.command("configure")
def provider_configure(
    ctx: typer.Context,
    provider_key: str | None = typer.Argument(None),
    credential_ref: str | None = typer.Option(None, "--credential-ref"),
    model: str | None = typer.Option(None, "--model"),
    project_id: UUID | None = typer.Option(None, "--project"),
    agent_id: UUID | None = typer.Option(None, "--agent"),
    expected_agent_revision: int | None = typer.Option(None, "--agent-revision", min=1),
    starter_agent_name: str | None = typer.Option(None, "--starter-name"),
    starter_agent_display_name: str | None = typer.Option(None, "--starter-display-name"),
    location: str | None = typer.Option(None, "--location"),
    option: list[str] | None = typer.Option(None, "--option"),
    custom_provider_key: str | None = typer.Option(None, "--custom-key"),
    custom_provider_name: str | None = typer.Option(None, "--custom-name"),
    custom_protocol: str | None = typer.Option(None, "--protocol"),
    custom_base_url: str | None = typer.Option(None, "--base-url"),
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    """重新验证 Provider 并发布新的不可变路由版本。"""

    _provider_add_command(
        ctx,
        provider_key,
        credential_ref,
        model,
        project_id,
        agent_id,
        expected_agent_revision,
        starter_agent_name,
        starter_agent_display_name,
        location,
        option,
        custom_provider_key,
        custom_provider_name,
        custom_protocol,
        custom_base_url,
        yes,
    )


@provider_app.command("list")
def provider_list(
    ctx: typer.Context,
    models: str | None = typer.Option(None, "--models", help="显示指定 Provider 的模型。"),
    limit: int = typer.Option(20, "--limit", min=1, max=100),
) -> None:
    state = _state(ctx)
    connections = _api(state, lambda client: client.list_provider_connections())
    if models is not None:
        connection = next(
            (item for item in connections if item.get("provider_key") == models),
            None,
        )
        if connection is None:
            _guard(
                state,
                lambda: (_ for _ in ()).throw(
                    CliError(
                        "PROVIDER_CONNECTION_NOT_FOUND",
                        f"Provider connection '{models}' was not found",
                        exit_code=2,
                    )
                ),
            )
            return
        rows = [
            {"provider": models, "model": value} for value in connection["allowed_models"][:limit]
        ]
        state.output().table(rows, title="Provider Models", columns=["provider", "model"])
        return
    rows = [
        {
            "provider": item["provider_key"],
            "name": item["display_name"],
            "protocol": item["protocol"],
            "revision": item["revision"],
            "verified": item["verified"],
            "enabled": item["enabled"],
            "credential": "configured",
            "agents": len(item.get("active_agents") or []),
        }
        for item in connections
    ]
    state.output().table(rows, title="Provider Connections")


@provider_app.command("test")
def provider_test(ctx: typer.Context, provider_key: str) -> None:
    state = _state(ctx)

    def operation() -> dict[str, Any]:
        profile = state.resolved_profile()
        with NicoApiClient(profile) as client:
            return ProviderOnboardingCoordinator(
                client,
                state.output(),
                service_bridge=None,
                interactive=False,
                prompt=typer.prompt,
                confirm=typer.confirm,
            ).test_existing(provider_key)

    result = _guard(state, operation)
    state.output().emit(result, title="Provider Test")


def _web_coordinator(state: AppState, client: NicoApiClient) -> WebCoordinator:
    profile = state.resolved_profile()
    return WebCoordinator(
        client,
        state.output(),
        service_bridge=ServiceBridge(profile),
        interactive=sys.stdin.isatty() and not state.json_mode,
        prompt=typer.prompt,
        confirm=typer.confirm,
    )


@web_app.command("configure")
def web_configure(
    ctx: typer.Context,
    provider: str | None = typer.Argument(None, help="brave 或 searxng。"),
    endpoint: str | None = typer.Option(None, "--endpoint"),
    credential_ref: str | None = typer.Option(None, "--credential-ref"),
    project_id: UUID | None = typer.Option(None, "--project"),
    agent_id: UUID | None = typer.Option(None, "--agent"),
    expected_agent_revision: int | None = typer.Option(None, "--agent-revision", min=1),
    starter_agent_name: str | None = typer.Option(None, "--starter-name"),
    starter_agent_display_name: str | None = typer.Option(None, "--starter-display-name"),
    safe_search: str = typer.Option("moderate", "--safe-search"),
    cache_ttl_seconds: int = typer.Option(900, "--cache-ttl", min=1, max=86_400),
    rate_limit_per_minute: int = typer.Option(20, "--rate-limit", min=1, max=10_000),
    allowed_domain: list[str] | None = typer.Option(None, "--allow-domain"),
    dns_resolver: str | None = typer.Option(None, "--dns-resolver"),
    yes: bool = typer.Option(False, "--yes", help="确认发布新的 AgentVersion。"),
) -> None:
    """验证 Web Provider 并原子发布到一个 Agent。"""

    state = _state(ctx)

    def operation() -> dict[str, Any]:
        profile = state.resolved_profile()
        with NicoApiClient(profile) as client:
            return _web_coordinator(state, client).configure(
                WebConfigureInput(
                    provider=provider,
                    endpoint_key=endpoint,
                    credential_ref=credential_ref,
                    project_id=str(project_id) if project_id else None,
                    agent_id=str(agent_id) if agent_id else None,
                    expected_agent_revision=expected_agent_revision,
                    starter_agent_name=starter_agent_name,
                    starter_agent_display_name=starter_agent_display_name,
                    safe_search=safe_search,
                    cache_ttl_seconds=cache_ttl_seconds,
                    rate_limit_per_minute=rate_limit_per_minute,
                    allowed_domains=allowed_domain or [],
                    dns_resolver=dns_resolver,
                    confirmed=yes,
                )
            )

    result = _guard(state, operation)
    state.output().emit(result, title="Nico Web")


@web_app.command("status")
def web_status(ctx: typer.Context) -> None:
    """显示 Web 授权、配置、凭据和最近探测状态。"""

    state = _state(ctx)

    def operation() -> dict[str, Any]:
        profile = state.resolved_profile()
        with NicoApiClient(profile) as client:
            return _web_coordinator(state, client).status()

    result = _guard(state, operation)
    if state.json_mode:
        state.output().emit(result)
        return
    probe = result.get("latest_probe") or {}
    state.output().table(
        [
            {
                "provider": result.get("provider") or "—",
                "state": result.get("diagnosis"),
                "authorized": result.get("authorized"),
                "credential": _web_credential_label(result),
                "probe": probe.get("status") or "not run",
                "agents": len(result.get("agents") or []),
            }
        ],
        title="Nico Web Status",
        columns=["provider", "state", "authorized", "credential", "probe", "agents"],
    )


def _web_credential_label(status: dict[str, Any]) -> str:
    if not status.get("secret_required"):
        return "not required"
    available = status.get("secret_available")
    if available is True:
        return "available"
    if available is False:
        return "unavailable"
    return "unknown"


@web_app.command("test")
def web_test(ctx: typer.Context) -> None:
    """探测当前 Web Provider；不会发布新版本。"""

    state = _state(ctx)

    def operation() -> dict[str, Any]:
        profile = state.resolved_profile()
        with NicoApiClient(profile) as client:
            return _web_coordinator(state, client).test()

    result = _guard(state, operation)
    state.output().emit(result, title="Nico Web Test")


@web_app.command("disable")
def web_disable(
    ctx: typer.Context,
    agent_id: UUID | None = typer.Option(None, "--agent"),
    yes: bool = typer.Option(False, "--yes", help="确认发布移除 Web 权限的新版本。"),
) -> None:
    """发布一个移除 Web 权限的新 AgentVersion。"""

    state = _state(ctx)

    def operation() -> dict[str, Any]:
        profile = state.resolved_profile()
        with NicoApiClient(profile) as client:
            return _web_coordinator(state, client).disable(
                agent_id=str(agent_id) if agent_id else None,
                confirmed=yes,
            )

    result = _guard(state, operation)
    state.output().emit(result, title="Nico Web")


@app.command("chat")
def chat_command(
    ctx: typer.Context,
    message: str | None = typer.Argument(None, help="单轮消息；省略时进入交互模式。"),
    project_id: UUID | None = typer.Option(None, "--project"),
    agent_id: str | None = typer.Option(None, "--agent", help="Agent exact name or UUID."),
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
        profile = state.resolved_profile()
        with NicoApiClient(profile) as client:
            runner = ChatRunner(
                client,
                state.output(),
                history_path=state.store().path.with_name("history"),
            )
            conversation = runner.resolve(
                project_id=str(project_id) if project_id else None,
                agent_id=agent_id,
                agent_version_id=str(agent_version_id) if agent_version_id else None,
                resume_id=str(resume_id) if resume_id else None,
                continue_latest=continue_latest,
                title=title,
                interactive=not state.json_mode and sys.stdin.isatty(),
                recent_agent_id=(
                    str(profile.recent_personal_agent_id)
                    if profile.recent_personal_agent_id
                    else None
                ),
            )
            if conversation.get("_cli_mode") == "personal":
                state.store().remember_personal_agent(
                    profile.name,
                    UUID(str(conversation["agent_id"])),
                )
            if read_only:
                history = runner.history(conversation["id"])
                if state.json_mode:
                    state.output().emit(
                        {
                            "conversation": runner.public_conversation(conversation),
                            "turns": history,
                        }
                    )
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
            if profile.tenant_id is not None:
                try:
                    tenant = client.get_tenant()
                    matches = str(tenant.get("id")) == str(profile.tenant_id)
                    checks.append(
                        {
                            "check": "tenant_access",
                            "status": "pass" if matches else "fail",
                            "detail": str(profile.tenant_id) if matches else "tenant mismatch",
                        }
                    )
                    failed = failed or not matches
                except CliError as exc:
                    checks.append({"check": "tenant_access", "status": "fail", "detail": exc.code})
                    failed = True
                try:
                    setup = client.setup_readiness()
                    overall = str(setup.get("overall") or "incomplete")
                    checks.append(
                        {
                            "check": "setup_overall",
                            "status": "pass" if overall == "full" else "warning",
                            "detail": overall,
                        }
                    )
                    for area in setup.get("areas") or []:
                        area_state = str(area.get("state") or "incomplete")
                        area_failed = area_state in {"blocked", "failed"}
                        checks.append(
                            {
                                "check": f"setup_{area.get('key')}",
                                "status": (
                                    "pass"
                                    if area_state == "ready"
                                    else ("fail" if area_failed else "warning")
                                ),
                                "detail": area_state,
                            }
                        )
                        failed = failed or area_failed
                except CliError as exc:
                    checks.append({"check": "setup_overall", "status": "fail", "detail": exc.code})
                    failed = True
                try:
                    web = _web_coordinator(state, client).status()
                    diagnosis = str(web.get("diagnosis") or "unconfigured")
                    web_good = diagnosis in {"ready", "unconfigured"}
                    checks.append(
                        {
                            "check": "web_configuration",
                            "status": "pass"
                            if diagnosis == "ready"
                            else ("warning" if diagnosis == "unconfigured" else "fail"),
                            "detail": diagnosis,
                        }
                    )
                    failed = failed or not web_good
                    if web.get("secret_required"):
                        credential = _web_credential_label(web)
                        checks.append(
                            {
                                "check": "web_credential",
                                "status": "pass"
                                if credential == "available"
                                else ("warning" if credential == "unknown" else "fail"),
                                "detail": credential,
                            }
                        )
                        failed = failed or credential == "unavailable"
                    latest_probe = web.get("latest_probe") or {}
                    checks.append(
                        {
                            "check": "web_probe",
                            "status": (
                                "pass"
                                if latest_probe.get("status") in {"succeeded", "activated"}
                                else "warning"
                            ),
                            "detail": latest_probe.get("error_code")
                            or latest_probe.get("status")
                            or "not run",
                        }
                    )
                except CliError as exc:
                    checks.append(
                        {"check": "web_configuration", "status": "fail", "detail": exc.code}
                    )
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
    service_command: str | None = typer.Option(None, "--service-command", hidden=True),
    install_root: str | None = typer.Option(None, "--install-root", hidden=True),
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
        if service_command is not None:
            values["service_command"] = service_command
        if install_root is not None:
            values["install_root"] = install_root
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
            "service_command": profile.service_command,
            "install_root": profile.install_root,
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
def project_list(
    ctx: typer.Context,
    include_system: bool = typer.Option(False, "--include-system"),
) -> None:
    state = _state(ctx)
    rows = _api(
        state,
        lambda client: (
            client.list_projects(include_system=True) if include_system else client.list_projects()
        ),
    )
    state.output().table(
        rows,
        title="Projects",
        columns=["id", "name", "kind", "status", "revision"],
    )


@project_app.command("get")
def project_get(ctx: typer.Context, project_id: str) -> None:
    state = _state(ctx)
    value = _api(
        state,
        lambda client: ProjectCli(client, state.output()).get(project_id),
    )
    state.output().emit(value, title="Project")


@project_app.command("new")
def project_new(
    ctx: typer.Context,
    name: str | None = typer.Argument(None),
    goal: str | None = typer.Option(None, "--goal"),
    lead: str | None = typer.Option(None, "--lead"),
    member: list[str] = typer.Option([], "--member"),
    description: str | None = typer.Option(None, "--description"),
    acceptance: str = typer.Option("{}", "--acceptance", help="JSON acceptance object."),
    cadence_seconds: int = typer.Option(3600, "--cadence-seconds", min=300, max=604800),
    no_supervision: bool = typer.Option(False, "--no-supervision"),
    confirmed: bool = typer.Option(False, "--yes"),
) -> None:
    """创建有一位 Lead 和零个或多个成员的协作 Project。"""
    state = _state(ctx)

    def operation() -> dict[str, Any]:
        interactive = not state.json_mode and sys.stdin.isatty()
        resolved_name = name or (typer.prompt("Project name") if interactive else None)
        resolved_goal = goal or (typer.prompt("Project goal") if interactive else None)
        if not resolved_name or not resolved_goal:
            raise CliError(
                "PROJECT_INPUT_REQUIRED",
                "project new requires NAME and --goal outside an interactive terminal",
                exit_code=2,
            )
        try:
            acceptance_value = json.loads(acceptance)
        except json.JSONDecodeError as exc:
            raise CliError(
                "PROJECT_ACCEPTANCE_INVALID", "--acceptance must be valid JSON", exit_code=2
            ) from exc
        if not isinstance(acceptance_value, dict):
            raise CliError(
                "PROJECT_ACCEPTANCE_INVALID", "--acceptance must be a JSON object", exit_code=2
            )
        if interactive and not confirmed and not typer.confirm("Create this Project?"):
            raise CliError(
                "PROJECT_CREATION_CANCELLED", "Project creation was cancelled", exit_code=2
            )
        profile = state.resolved_profile()
        with NicoApiClient(profile) as client:
            value = ProjectCli(
                client,
                state.output(),
                interactive=interactive,
                prompt=typer.prompt,
            ).create(
                name=resolved_name,
                goal=resolved_goal,
                lead=lead,
                members=member,
                description=description,
                acceptance=acceptance_value,
                cadence_seconds=None if no_supervision else cadence_seconds,
            )
        state.store().remember_project(profile.name, UUID(str(value["project"]["id"])))
        return value

    value = _guard(state, operation)
    state.output().emit(value, title="Project Created")


@project_app.command("status")
def project_status(ctx: typer.Context, project: str) -> None:
    """查看 Project、成员和稳定 Session 的当前状态。"""
    state = _state(ctx)
    value = _api(state, lambda client: ProjectCli(client, state.output()).status(project))
    state.output().emit(value, title="Project Status")


@project_app.command("members")
def project_members(ctx: typer.Context, project: str) -> None:
    """列出 Project 的 Lead 和成员。"""
    state = _state(ctx)
    value = _api(state, lambda client: ProjectCli(client, state.output()).status(project))
    state.output().table(
        value["members"],
        title="Project Members",
        columns=["agent_id", "role", "status", "revision", "created_at"],
    )


def _run_project_chat(
    state: AppState,
    *,
    project: str,
    agent: str | None,
    message: str | None,
    read_only: bool,
) -> None:
    if state.json_mode and message is None and not read_only:
        raise CliError(
            "JSON_INTERACTIVE_UNSUPPORTED",
            "--json project chat requires --message or --read-only",
            exit_code=2,
        )
    profile = state.resolved_profile()
    with NicoApiClient(profile) as client:
        project_cli = ProjectCli(client, state.output())
        workspace = project_cli.workspace(project, agent_reference=agent)
        conversation = {
            **workspace["conversation"],
            "_cli_mode": "project",
            "_cli_project_id": str(workspace["project"]["id"]),
            "_cli_project_session_id": str(workspace["session"]["id"]),
        }
        runner = ChatRunner(
            client,
            state.output(),
            history_path=state.store().path.with_name("history"),
        )
        state.store().remember_project(profile.name, UUID(str(workspace["project"]["id"])))
        effective_read_only = read_only or bool(workspace["read_only"])
        if effective_read_only:
            turns = runner.history(conversation["id"])
            if state.json_mode:
                state.output().emit({"workspace": workspace, "turns": turns})
            else:
                runner.run_interactive(conversation, read_only=True)
            return
        if message is not None:
            result = runner.submit(conversation, message)
            if state.json_mode:
                state.output().emit({"workspace": workspace, **result})
            return
        runner.run_interactive(conversation, read_only=False)


@project_app.command("open")
def project_open(
    ctx: typer.Context,
    project: str,
    message: str | None = typer.Option(None, "--message"),
    read_only: bool = typer.Option(False, "--read-only"),
) -> None:
    """进入 Project Lead 的稳定 Session。"""
    state = _state(ctx)
    _guard(
        state,
        lambda: _run_project_chat(
            state,
            project=project,
            agent=None,
            message=message,
            read_only=read_only,
        ),
    )


@project_app.command("session")
def project_session(
    ctx: typer.Context,
    project: str,
    agent: str = typer.Option(..., "--agent"),
    message: str | None = typer.Option(None, "--message"),
    read_only: bool = typer.Option(False, "--read-only"),
) -> None:
    """进入指定成员的稳定 Session；暂停或归档后自动只读。"""
    state = _state(ctx)
    _guard(
        state,
        lambda: _run_project_chat(
            state,
            project=project,
            agent=agent,
            message=message,
            read_only=read_only,
        ),
    )


@project_app.command("timeline")
def project_timeline(
    ctx: typer.Context,
    project: str,
    agent: str | None = typer.Option(None, "--agent"),
    after_sequence: int = typer.Option(0, "--after", min=0),
    limit: int = typer.Option(100, "--limit", min=1, max=500),
) -> None:
    """查看成员 Session 的可审计工作时间线。"""
    state = _state(ctx)
    value = _api(
        state,
        lambda client: ProjectCli(client, state.output()).timeline(
            project,
            agent_reference=agent,
            after_sequence=after_sequence,
            limit=limit,
        ),
    )
    if state.json_mode:
        state.output().emit(value)
    else:
        ProjectRenderer(state.output()).timeline(value["timeline"]["entries"])


@project_app.command("member-add")
def project_member_add(
    ctx: typer.Context,
    project: str,
    agent: str,
    expected_revision: int | None = typer.Option(None, "--expected-revision", min=1),
) -> None:
    state = _state(ctx)
    value = _api(
        state,
        lambda client: ProjectCli(client, state.output()).add_member(
            project, agent, expected_project_revision=expected_revision
        ),
    )
    state.output().emit(value, title="Project Member Added")


@project_app.command("member-state")
def project_member_state(
    ctx: typer.Context,
    project: str,
    agent: str,
    target: str = typer.Option(..., "--state", help="active, paused, or removed"),
    reason: str | None = typer.Option(None, "--reason"),
    expected_project_revision: int | None = typer.Option(
        None, "--expected-project-revision", min=1
    ),
    expected_member_revision: int | None = typer.Option(None, "--expected-member-revision", min=1),
) -> None:
    state = _state(ctx)
    if target not in {"active", "paused", "removed"}:
        raise typer.BadParameter("--state must be active, paused, or removed")
    value = _api(
        state,
        lambda client: ProjectCli(client, state.output()).set_member_state(
            project,
            agent,
            target=target,
            reason=reason,
            expected_project_revision=expected_project_revision,
            expected_member_revision=expected_member_revision,
        ),
    )
    state.output().emit(value, title="Project Member Updated")


@project_app.command("lead")
def project_lead(
    ctx: typer.Context,
    project: str,
    agent: str,
    expected_revision: int | None = typer.Option(None, "--expected-revision", min=1),
) -> None:
    state = _state(ctx)
    value = _api(
        state,
        lambda client: ProjectCli(client, state.output()).replace_lead(
            project, agent, expected_project_revision=expected_revision
        ),
    )
    state.output().emit(value, title="Project Lead Replaced")


@project_app.command("tasks")
def project_tasks(
    ctx: typer.Context,
    project: str,
    limit: int = typer.Option(100, "--limit", min=1, max=500),
) -> None:
    """汇总各成员 Session 的持久化 Task 事件。"""
    state = _state(ctx)
    value = _api(
        state,
        lambda client: ProjectCli(client, state.output()).tasks(project, limit=limit),
    )
    if state.json_mode:
        state.output().emit(value)
    else:
        state.output().table(
            value["tasks"],
            title="Project Tasks",
            columns=["sequence", "agent_name", "event_type", "facts", "links"],
        )


@project_app.command("sync")
def project_sync(ctx: typer.Context, project: str) -> None:
    """请求一次幂等、可恢复的 Lead 同步周期。"""
    state = _state(ctx)
    value = _api(state, lambda client: ProjectCli(client, state.output()).sync(project))
    state.output().emit(value, title="Project Sync Requested")


def _parse_cadence(value: str) -> int | None:
    normalized = value.strip().lower()
    if normalized in {"off", "none", "disabled"}:
        return None
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    suffix = normalized[-1:] if normalized else ""
    try:
        if suffix in multipliers:
            return int(normalized[:-1]) * multipliers[suffix]
        return int(normalized)
    except ValueError as exc:
        raise CliError(
            "PROJECT_CADENCE_INVALID",
            "cadence must be off or an integer with s, m, h, or d suffix",
            exit_code=2,
        ) from exc


@project_app.command("cadence")
def project_cadence(
    ctx: typer.Context,
    project: str,
    value: str,
    reason: str | None = typer.Option(None, "--reason"),
    expected_revision: int | None = typer.Option(None, "--expected-revision", min=1),
) -> None:
    """设置同步周期，例如 30m、2h、1d 或 off。"""
    state = _state(ctx)
    cadence_seconds = _parse_cadence(value)
    result = _api(
        state,
        lambda client: ProjectCli(client, state.output()).cadence(
            project,
            cadence_seconds=cadence_seconds,
            expected_project_revision=expected_revision,
            reason=reason,
        ),
    )
    state.output().emit(result, title="Project Cadence Updated")


@project_app.command("cycles")
def project_cycles(ctx: typer.Context, project: str) -> None:
    """查看监督周期的数据库事实和 Lead 摘要。"""
    state = _state(ctx)
    value = _api(state, lambda client: ProjectCli(client, state.output()).cycles(project))
    if state.json_mode:
        state.output().emit(value)
    else:
        ProjectRenderer(state.output()).cycles(value["cycles"])


@project_app.command("guide")
def project_guide(
    ctx: typer.Context,
    project: str,
    content: str,
    agent: str | None = typer.Option(None, "--agent"),
    run_id: str | None = typer.Option(None, "--run"),
    expected_run_revision: int | None = typer.Option(None, "--expected-run-revision", min=1),
) -> None:
    """在安全模型边界向成员当前 Run 提交一次性指导。"""
    state = _state(ctx)
    value = _api(
        state,
        lambda client: ProjectCli(client, state.output()).guide(
            project,
            agent_reference=agent,
            content=content,
            run_reference=run_id,
            expected_run_revision=expected_run_revision,
        ),
    )
    state.output().emit(value, title="Run Guidance Pending")


@project_app.command("escalate")
def project_escalate(
    ctx: typer.Context,
    project: str,
    content: str,
    agent: str | None = typer.Option(None, "--agent"),
    max_steps: int = typer.Option(64, "--max-steps", min=1, max=10_000),
    token_budget: int | None = typer.Option(None, "--token-budget", min=1),
    timeout_seconds: int | None = typer.Option(None, "--timeout-seconds", min=1, max=604_800),
) -> None:
    """把范围或优先级变更升级到 Lead Session 重新规划。"""
    state = _state(ctx)
    value = _api(
        state,
        lambda client: ProjectCli(client, state.output()).escalate(
            project,
            agent_reference=agent,
            content=content,
            max_steps=max_steps,
            token_budget=token_budget,
            timeout_seconds=timeout_seconds,
        ),
    )
    state.output().emit(value, title="Project Change Escalated")


@project_app.command("interventions")
def project_interventions(
    ctx: typer.Context,
    project: str,
    agent: str | None = typer.Option(None, "--agent"),
    run_id: str | None = typer.Option(None, "--run"),
) -> None:
    """查看成员 Run 上指导的 pending/consumed/terminal 状态。"""
    state = _state(ctx)
    value = _api(
        state,
        lambda client: ProjectCli(client, state.output()).interventions(
            project,
            agent_reference=agent,
            run_reference=run_id,
        ),
    )
    if state.json_mode:
        state.output().emit(value)
    else:
        state.output().table(
            value["interventions"],
            title="Run Interventions",
            columns=["id", "kind", "status", "revision", "content", "created_at"],
        )


@project_app.command("withdraw")
def project_withdraw(
    ctx: typer.Context,
    project: str,
    intervention_id: str,
    agent: str | None = typer.Option(None, "--agent"),
    run_id: str | None = typer.Option(None, "--run"),
    expected_revision: int | None = typer.Option(None, "--expected-revision", min=1),
    reason: str | None = typer.Option(None, "--reason"),
) -> None:
    """按 revision 撤回尚未消费的指导。"""
    state = _state(ctx)
    value = _api(
        state,
        lambda client: ProjectCli(client, state.output()).withdraw(
            project,
            agent_reference=agent,
            run_reference=run_id,
            intervention_id=intervention_id,
            expected_intervention_revision=expected_revision,
            reason=reason,
        ),
    )
    state.output().emit(value, title="Run Guidance Withdrawn")


@project_app.command("cancel")
def project_cancel(
    ctx: typer.Context,
    project: str,
    agent: str | None = typer.Option(None, "--agent"),
    run_id: str | None = typer.Option(None, "--run"),
    expected_revision: int | None = typer.Option(None, "--expected-revision", min=1),
) -> None:
    """按 revision 取消成员 Session 中的活动 Run 树。"""
    state = _state(ctx)
    value = _api(
        state,
        lambda client: ProjectCli(client, state.output()).cancel(
            project,
            agent_reference=agent,
            run_reference=run_id,
            expected_run_revision=expected_revision,
        ),
    )
    state.output().emit(value, title="Project Run Cancelled")


@agent_app.command("list")
def agent_list(ctx: typer.Context) -> None:
    state = _state(ctx)
    rows = _api(state, lambda client: client.list_agents())
    state.output().table(
        rows,
        title="Agents",
        columns=["id", "name", "display_name", "status", "current_version_id"],
    )


def _capability_coordinator(
    state: AppState,
    client: NicoApiClient,
) -> CapabilityCoordinator:
    return CapabilityCoordinator(
        client,
        state.output(),
        interactive=sys.stdin.isatty() and not state.json_mode,
        prompt=typer.prompt,
        confirm=typer.confirm,
    )


@agent_app.command("capabilities")
def agent_capabilities(
    ctx: typer.Context,
    agent_id: UUID,
    profile: str | None = typer.Option(None, "--profile"),
    tool: list[str] | None = typer.Option(None, "--tool"),
    skill: list[UUID] | None = typer.Option(None, "--skill"),
    select_all: bool = typer.Option(False, "--all", help="选择全部当前可用能力。"),
    clear_all: bool = typer.Option(False, "--clear", help="清除全部可选能力。"),
    accept_risk: list[str] | None = typer.Option(None, "--accept-risk"),
    yes: bool = typer.Option(False, "--yes", help="确认发布预览。"),
) -> None:
    """为现有 Agent 发布一个新的不可变能力版本。"""

    state = _state(ctx)

    def operation() -> dict[str, Any]:
        with NicoApiClient(state.resolved_profile()) as client:
            return _capability_coordinator(state, client).publish(
                CapabilityInput(
                    agent_id=str(agent_id),
                    profile=profile,
                    tool_refs=tool or [],
                    skill_version_ids=[str(value) for value in (skill or [])],
                    select_all=select_all,
                    clear_all=clear_all,
                    accepted_risks=accept_risk or [],
                    confirmed=yes,
                )
            )

    state.output().emit(_guard(state, operation), title="Agent Capabilities")


@agent_app.command("create")
def agent_create(
    ctx: typer.Context,
    name: str,
    display_name: str | None = typer.Option(None, "--display-name"),
    source_version: UUID | None = typer.Option(None, "--source-version"),
    profile: str | None = typer.Option(None, "--profile"),
    tool: list[str] | None = typer.Option(None, "--tool"),
    skill: list[UUID] | None = typer.Option(None, "--skill"),
    select_all: bool = typer.Option(False, "--all"),
    clear_all: bool = typer.Option(False, "--clear"),
    accept_risk: list[str] | None = typer.Option(None, "--accept-risk"),
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    """从现有已发布模型版本创建 Starter Agent 并选择能力。"""

    state = _state(ctx)

    def operation() -> dict[str, Any]:
        interactive = sys.stdin.isatty() and not state.json_mode
        resolved_display = display_name
        resolved_source = source_version
        with NicoApiClient(state.resolved_profile()) as client:
            if resolved_display is None and interactive:
                resolved_display = typer.prompt("Starter Agent display name", default=name)
            if resolved_source is None and interactive:
                agents = [
                    item
                    for item in client.list_agents()
                    if item.get("current_version_id") and item.get("status") != "archived"
                ]
                state.output().table(
                    agents,
                    title="Published Model Sources",
                    columns=["name", "display_name", "current_version_id", "status"],
                )
                resolved_source = UUID(typer.prompt("Source AgentVersion ID").strip())
            return _capability_coordinator(state, client).publish(
                CapabilityInput(
                    starter_agent_name=name,
                    starter_agent_display_name=resolved_display,
                    source_agent_version_id=(
                        str(resolved_source) if resolved_source is not None else None
                    ),
                    profile=profile,
                    tool_refs=tool or [],
                    skill_version_ids=[str(value) for value in (skill or [])],
                    select_all=select_all,
                    clear_all=clear_all,
                    accepted_risks=accept_risk or [],
                    confirmed=yes,
                )
            )

    state.output().emit(_guard(state, operation), title="Agent Created")


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
