# Goal L Handoff：外部 Adapter 兼容性与全量收口

日期：2026-07-18 UTC  
状态：Implemented（95%）  
下一阶段：迁移计划 Goal G–L 已全部交付

## 本阶段结论

Goal L 已将 Hermes 收口为默认关闭、显式启用的 Runtime Provider v2 Adapter。Nico Native 仍是默认且可独立运行的 Runtime；默认镜像、Provider registry、Compose 服务和状态卷都不包含 Hermes。

Hermes Adapter 保留 0.18.2 版本固定、每 Run HOME、进程组取消、历史 session resume、脱敏导出和仅 Nico MCP 的边界，现直接返回 v2 terminal outcome。它不声明 Native planning、reflection、delegation 或 Artifact 能力。未启用、命令缺失、版本不匹配或恢复不兼容都会失败关闭，不会回退到 Native。

## 兼容与数据合同

- Alembic migration：`20260718_0016_provider_compatibility.py`。
- RuntimeSession 新增 Provider 解析来源、legacy 标记和冻结 compatibility metadata。
- 已持久化的 RuntimeSession Provider/version/protocol 始终是恢复权威事实；migration 与 Worker 都不改写历史身份。
- 历史 `run_config`、`model_config` 和 Mock 缺省解析会记录 `LegacyRuntimeProviderResolved` Event/Audit，弃用窗口为 `0.2.x`–`0.3.x`，计划在 `0.4.0` 移除隐式 resolver。
- Runtime API 公开非敏感的 resolution/compatibility 摘要；不公开 Provider state、checkpoint 正文、知识正文或凭据。

## 部署与 Console

- `docker compose up` 不安装或注册 Hermes。
- 显式 Hermes AgentVersion 可在停止默认 Worker 后，使用 `docker compose --profile hermes up --detach --build worker-hermes`。
- `goal-l` profile 是无外部凭据的 fake/local CLI 合同验收，不是用户部署方式。
- Console 新增只读 Run Inspector，固定按状态/结果、步骤/用量、Plan、Child tree、Message/Artifact、Budget/Audit 展示。
- Inspector 支持深链、键盘和辅助标签，覆盖 loading/empty/error/partial/cancelled/redacted；恶意 HTML 作为纯文本渲染，不显示隐藏推理或 Secret。

## 验收结果

- Ruff 与格式检查：通过。
- 后端单元测试：234 项通过。
- 前端组件测试：11 项通过；TypeScript 与 production build 通过。
- 真实 PostgreSQL/pgvector、Redis、MinIO 集成测试：75 项通过。
- Alembic 从 0016 head 完整 downgrade 到 base 再 upgrade：通过。
- Markdown lint：21 份公开文档 0 错误；本地链接、发布面内部路径与凭据模式扫描通过。
- 默认 Compose 无 Hermes；Goal G–L 全部 hermetic/fault-injection/local contract E2E 通过。
- 最终证据：`artifacts/goals/goal-l/20260718T201512Z/`，134 个文件的 SHA-256 manifest 校验通过。

## 仍需部署方完成的外部验收

仓库内的 Goal A–L 实现任务已完成。Goal G–L 保留 `Implemented 95%` 是因为操作者未提供真实外部模型 endpoint、model name、credential ref 和 Hermes Provider 凭据。后续如需升级为 `Verified`，应在部署方受控 Secret 环境执行 credentialed acceptance，只校验结构事实、usage 状态和零 Secret 泄露，不应将凭据写入命令、日志或证据。
