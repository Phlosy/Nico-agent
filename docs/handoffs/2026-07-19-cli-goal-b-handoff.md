# CLI Goal B Handoff：基础客户端

日期：2026-07-19 UTC  
状态：Verified（100%）  
下一阶段：CLI Goal C——Conversation 领域与 chat 基础能力

## 1. 本阶段目标

建立可安装、可配置、只通过 HTTP 调用 Nico 的正式 `nico` 入口，交付 profile、API client、统一输出、诊断命令和现有 Project/Agent/Task/Run 的基础查询，不提前实现 Conversation 或交互 chat。

## 2. 实际完成内容

- 新增 `nico = nico_agent.cli.app:run` 和 `python -m nico_agent.cli` 入口。
- 锁定 Typer、Rich、prompt_toolkit、platformdirs；复用 httpx。
- 实现 platformdirs 默认路径、TOML 多 profile、CLI/env 优先级、原子写入、配置目录 0700 与文件 0600。
- 配置只保存 token 环境变量名；`NICO_API_TOKEN` 和引用 token 只在进程内存解析。
- 实现租户/actor/request ID/Bearer header、超时/网络/HTTP/非 JSON 错误映射和安全 request ID 回显。
- 实现 human、JSON、stderr error、`NO_COLOR`/`--no-color` 输出合同。
- 实现 `version`、`health`、`doctor`、`config path/list/show/set/use/delete`。
- 实现 `project list/get`、`agent list/get/versions`、`task get`、`run get/runtime/events`。
- 新增 17 项 CLI 单元测试和真实 Compose API 子进程 E2E。
- 修正 Demo 对 Artifact 能力的过时描述；Mock Demo 只说明本次未生成 Artifact。

## 3. 未完成内容

`nico chat`、`nico exec`、`nico run watch`、Conversation/Turn、CLI SSE parser、slash commands、coin-cat header、附件、summary 和 Tool Approval 均未在本阶段实现。它们仍明确属于 CLI-C–F。

正式 API Key/JWT 仍是平台既有缺口；CLI 已提供环境引用的 Bearer token 传输，但不把开发 Tenant header 描述为认证。

## 4. 新增文件

- `backend/src/nico_agent/cli/{__init__,__main__,app,client,config,errors,output}.py`
- `backend/tests/unit/test_cli_{app,client,config,output}.py`
- `docs/cli-development.md`
- `scripts/e2e-cli-goal-b.sh`
- `scripts/verify-cli-goal-b.sh`
- 本 Handoff 与 `artifacts/goals/cli-goal-b/20260719T101257Z/`。

## 5. 修改文件

- `backend/pyproject.toml`：CLI 依赖、entry point 和 Ruff Typer 配置。
- `README.md`、`docs/cli.md`、`docs/testing.md`：安装、使用、安全与验收说明。
- `docs/progress/goal-status.md`、`docs/progress/feature-matrix.md`：CLI-B 实现事实。
- `scripts/demo.sh`：修正 Artifact 状态文字。
- `scripts/verify-cli-goal-a.sh`：移除会被后续正常实现破坏的历史“无 CLI 代码”断言；Goal A 的历史证据不变。

## 6. 数据库变更

无。Alembic head 仍为 `20260718_0016`。CLI profile 仅存于用户配置目录，不进入服务端数据库。

## 7. API 变更

无。CLI-B 只消费既有公开 API，没有为 CLI 建立旁路端点。Task/Run/Agent 的服务端行为未改变。

## 8. CLI 命令变更

新增：

```text
nico --version
nico version [--server]
nico health [--live]
nico doctor
nico config path|list|show|set|use|delete
nico project list|get
nico agent list|get|versions
nico task get
nico run get|runtime|events
```

全局支持 `--profile`、`--config-file`、`--api-url`、`--tenant-id`、`--actor-id`、`--json` 和 `--no-color`。

## 9. 测试命令

```bash
.venv/bin/pytest backend/tests/unit/test_cli_*.py
scripts/e2e-cli-goal-b.sh
scripts/verify-cli-goal-b.sh
```

## 10. 测试结果

- Ruff 和格式：通过。
- 后端单元测试：251 项通过，其中 CLI 新增 17 项。
- 前端测试：11 项通过；TypeScript 和 production build 通过。
- 真实 PostgreSQL/pgvector、Redis、MinIO 集成测试：75 项通过。
- CLI 真实 E2E：profile set/use、0600、health、server version、doctor、Project、Agent/Version、Task、Run、Runtime、17 个 Event、JSON、无色和缺租户错误全部通过。
- 文档链接、发布面扫描、Markdown lint、Shell 语法、Compose 配置和 diff check：通过。

## 11. 已知问题

- 当前服务端没有正式调用方认证，生产部署仍不可仅依赖 Tenant header。
- API 目前没有 Task/Run 列表，因此 CLI-B 只能按 ID 查询 Task/Run；不得伪造 list。
- Rich 当前是基础通用 renderer；Event/Plan/Tool/Artifact/usage 专用视图和 coin-cat 属于 CLI-D。
- 当前 client 是同步 REST；流式 SSE 与断线续读属于 CLI-C。
- 配置文件权限检查采用 POSIX mode；其他操作系统的 ACL 需要后续平台验收。

## 12. 当前架构

`nico` 加载本地非秘密 profile，解析环境中的 token，通过 `NicoApiClient` 发送 REST 请求；所有资源命令要求租户上下文。服务端 API、PostgreSQL 和 Worker 仍是唯一业务与执行权威。输出层只渲染响应事实，不运行 Agent、不读取数据库、不持有 Run 最终状态。

## 13. 下一阶段建议

CLI Goal C 应按 ADR-0012：

1. 新增 Conversation/ConversationTurn 和现有 ContextSnapshot 可空关联迁移；
2. 建立同租户/RLS/不可变 AgentVersion 约束；
3. 在一个事务内创建 Turn→Task→Run；
4. 扩展 client 和最小 `nico chat`；
5. 实现 SSE parser、Last-Event-ID、resume/continue/history 与 Ctrl+C 权威取消；
6. 保持 summary、附件和完整视觉在后续 Goal。

## 14. 禁止重复实现

- 不要重建 CLI 配置、HTTP client、错误或基础输出层。
- 不要让 CLI 导入 ControlPlane、数据库、Runtime、Worker 或 Tool 实现。
- 不要保存明文 token，或把 Tenant header 声称为身份认证。
- 不要为缺少的 Task/Run list 写 mock 数据。
- 不要把 RuntimeSession、AgentMessage 或 CLI 输入历史当作 Conversation。

## 15. 验收证据

最终证据目录：`artifacts/goals/cli-goal-b/20260719T101257Z/`。包含环境、命令、单元/集成/前端日志、真实 CLI E2E JSON、human/no-color 渲染、预期错误、验收摘要和 SHA-256 manifest。
