# CLI Goal B 验收摘要

验收时间：2026-07-19 UTC  
结论：Verified（100%）

## 验收结果

- 安装后的 `nico 0.2.0` 入口可运行，并且 `python -m nico_agent.cli` 使用同一入口。
- profile 支持默认目录、自定义配置路径、选择/增删、CLI 与环境覆盖；配置文件实测为 `0600`。
- 配置只记录 token 环境变量名称；本阶段证据与配置中没有保存明文 token。
- HTTP client 只调用公开 API，并发送 tenant、actor、request ID 和可选 Bearer token；CLI 未导入数据库、ControlPlane、Runtime、Worker 或 Tool 实现。
- health、doctor、version、config、Project、Agent/AgentVersion、Task、Run、Runtime 与 Event 命令均对真实 Compose API 通过。
- human、单 JSON 文档、stderr JSON error、`--no-color`/`NO_COLOR` 均通过；无色样例不含 ANSI 转义序列。
- 真实 Mock Runtime Run 完成，CLI 查询到 completed Run、Runtime、Task 和 17 个持久化 Event，最终事件为 `RunCompleted`。
- 缺少 tenant 的资源命令稳定返回退出码 2 和 `TENANT_CONTEXT_REQUIRED`，stdout 为空。

## 回归结果

- 后端单元测试：251 passed（CLI 新增 17 项）。
- 前端：3 个测试文件、11 项测试通过；TypeScript 与 production build 通过。
- 真实基础设施集成：75 passed，包含完整 Alembic downgrade/upgrade 往返。
- 文档检查：65 个 Markdown 文件、19 个发布面文件通过；Markdown lint 零错误。
- Shell 语法、Compose 配置、Ruff、格式、`git diff --check` 均通过。
- CLI Goal A 回归验收通过。

## 范围边界

本阶段没有实现 Conversation、`nico chat`、`nico exec`、`nico run watch`、CLI SSE client、附件、summary、coin-cat 终端渲染或 Tool Approval。这些能力仍分别属于 CLI Goal C–F，没有以占位命令或 mock 数据冒充完成。

