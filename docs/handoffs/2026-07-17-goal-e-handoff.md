# Goal E Handoff：Tool Gateway 与 Sandbox

## 1. 本阶段目标

在 Goal D 的 Runtime/Worker 基础上建立平台唯一工具执行边界：版本化 Tool Registry/Definition、不可变 ToolCall、租户与 AgentVersion 权限交集、Schema/Secret/幂等/超时/重试/取消/审计，以及安全的文件、报告、HTTP、只读数据库和 Python 容器工具；Runtime 和 Hermes 不能绕过 Gateway。

## 2. 实际完成内容

- `ToolDefinitionSpec`、精确 `name@version` Registry、内容/实现 Hash 和租户 Definition 快照；
- ToolCall/RunStep/Event/Audit 权威执行记录，终态数据库 trigger 禁止更新或删除；
- Run 租约复核、默认拒绝、Tenant∩AgentVersion 冻结权限、Schema、Secret 引用/脱敏、幂等冲突、受控重试、timeout/cancel 与迟到结果拒绝；
- tenant/run 工作区内的原子文件读写和 Markdown/JSON 报告；
- 只允许 GET/HEAD 的 HTTP 工具，包含域名白名单、全部 DNS/IP、重定向、连接固定和 DNS rebinding 防护；
- 只接受管理员 Secret DSN、单条参数化 SELECT/WITH、只读事务/角色与行/时间/输出限制的 PostgreSQL 查询工具；
- 独立认证 Sandbox Runner 与固定 digest 一次性 Python 容器：非 root、无网络、只读 rootfs、drop capabilities、CPU/内存/PID/时间/tmpfs/输出限制和强制清理；
- Runtime `platform_tools` capability、规范化 tool intent/outcome、Mock 工具调用；
- 每 Run 的 Nico MCP broker/server；Hermes 0.18.2 只启用 Nico MCP，禁用 native terminal/web/browser/file/memory/skills/delegate，隔离 `HERMES_HOME` 并清理短期凭据；
- ToolDefinition/ToolCall 查询 API、Compose 状态卷初始化、Goal E E2E 与一键验收。

阶段提交：

- `08953a4`：冻结 Tool/Sandbox 合同、ADR 与计划；
- `920802f`：版本化 Registry、ToolDefinition/ToolCall 与迁移；
- `03ed5c1`：租约、授权、Schema、Secret、幂等、重试/取消与审计 Gateway；
- `941e4d8`：Run 工作区文件与报告隔离；
- `206bac9`：固定 DNS/IP 的 HTTP 只读工具；
- `03c4e0c`：有界只读数据库查询；
- `67066e3`：独立 Python Sandbox Runner；
- `dc9df1b`：Runtime/Mock/Hermes MCP/API 接入；
- `b8703b1`：Compose Tool/Sandbox E2E、文档和验收脚本；
- `7cd4416`：隔离 Goal C 状态机 E2E 与 Runtime Worker，消除验收竞态。

## 3. 未完成内容

Goal E 范围内无未完成项。以下内容按任务书明确留给后续 Goal：Memory/Skill 与候选成长（F）、Team/Role/Workflow（G）、Plugin 加载（H）、量化策略/交易工具（I）、业务 Web Console（J）、正式 Approval/认证/SSE/SDK 与最终安全治理（K）。ModelCall 持久化也尚未实现。

真实 Hermes 模型推理因没有外部模型凭据未执行；已完成真实 Hermes 0.18.2 MCP initialize/tools/list 发现，不把边界兼容冒充模型质量。

## 4. 新增文件

- `backend/migrations/versions/20260717_0004_versioned_tools.py`；
- `backend/migrations/versions/20260717_0005_run_tool_policy.py`；
- `backend/src/nico_agent/tools/` 下合同、错误、Registry、Policy、Secret、Gateway 与六个内置 Executor；
- `backend/src/nico_agent/sandbox/` 下合同、Docker Engine Runner 与 FastAPI 服务；
- `backend/src/nico_agent/mcp/` broker 与 stdio MCP server；
- `backend/src/nico_agent/runtime/tools.py`；
- Tool/Sandbox/MCP 单元与真实集成测试；
- `docs/tool-gateway.md`、ADR-0009、Goal E 计划、本 Handoff；
- `scripts/e2e-goal-e.sh`、`scripts/verify-goal-e.sh`；
- `artifacts/goals/goal-e/20260717T063841Z/` 验收证据。

## 5. 修改文件

主要修改 `domain/models.py`、`domain/states.py`、`database.py`、`control_plane.py`、API schemas/router、Runtime contracts/executor/mock/hermes、`worker.py`、`config.py`、`docker-compose.yml`、bootstrap/E2E/测试脚本，以及 README、architecture/runtime/api/development/testing、Goal Status 与 Feature Matrix。既有 Goal C E2E 只增加“控制面阶段停止 Worker”这一职责隔离。

## 6. 数据库变更

- `tool_definitions`：租户内精确版本唯一，保存 Schema、权限、隔离、风险、重试、输出限制、content/implementation Hash、状态与 revision；
- `tool_calls`：绑定 tenant/run/run_step/definition，保存脱敏参数、arguments Hash、幂等键、caller、执行租约、attempts、result/error/usage 与时间；
- `runtime_sessions.tool_policy_snapshot`：保存 Run 不可变权限快照；
- 复合租户外键、FORCE RLS、运行角色授权与索引；
- trigger 阻止终态 ToolCall 更新/删除，迁移已完成 head→base→head 回放。

## 7. API 变更

- `GET /api/v1/tool-definitions`：当前租户已实例化工具版本；
- `GET /api/v1/runs/{run_id}/tool-calls`：按 Run 读取脱敏调用、尝试和结果；
- 不提供直接工具执行 API，也不公开 `execution_lease_token`；
- 现有 Runtime trajectory 增加 `tool.call.started/completed` 事件。

## 8. 配置变更

新增/使用 `NICO_WORKSPACE_*`、`NICO_HTTP_*`、`NICO_DATABASE_TOOL_*`、`NICO_SANDBOX_*`、`NICO_HERMES_STATE_ROOT`。生产必须更换 Runner token；sandbox image 必须为固定 SHA-256 digest。数据库 Secret 环境名必须匹配 `NICO_TOOL_SECRET_*`。Hermes 运行环境需安装兼容 0.18.2 的 MCP client；开发验收固定 `mcp==1.26.0`、`starlette==1.0.1`。

## 9. 测试命令

```bash
scripts/test.sh
scripts/test-integration.sh
scripts/e2e-goal-c.sh
scripts/e2e-goal-d.sh
scripts/e2e-goal-e.sh
NICO_EVIDENCE_DIR=artifacts/goals/goal-e/20260717T063841Z scripts/verify-goal-e.sh
```

## 10. 测试结果

- 后端单元：138 passed；
- 前端：7 passed，TypeScript/Vite production build passed；
- 真实集成：31 passed，包含真实 PostgreSQL、Docker sandbox 与 Hermes 0.18.2 MCP；
- Goal C 控制面/RLS E2E：passed；
- Goal D Runtime/Worker E2E：passed；
- Goal E API→Worker→Gateway→文件/报告/Python Runner→ToolCall/Event/Audit/Trajectory E2E：passed；
- Python E2E 结果 UID 65534、计算 42；无残留 `nico.sandbox=true` 容器；
- 固定 sandbox 镜像 inspect 已归档。

第一次验收 `20260717T063652Z` 正确失败并保留：Worker 抢占 Goal C 手工 Run 导致 409。`7cd4416` 修复职责竞态后，完整重跑 `20260717T063841Z` 全绿。

## 11. 已知问题

- 没有真实外部模型凭据，因此不声称 Hermes 推理成功或盈利能力；
- Docker socket 仍是宿主级高权限边界；生产应把 Runner 放在独立节点/更强隔离环境并轮换 token；
- 当前 Secret resolver 只有严格环境引用，未接 Vault/KMS；
- HTTP 是通用只读访问，数据库是只读查询；没有外部写、交易、浏览器或终端工具；
- Role/Plugin/Approval 尚未实现，不能用 Prompt 或 Tenant Header 代替。

## 12. 当前架构

```text
Client -> FastAPI -> PostgreSQL Pending Run
                       |
                 leased non-root Worker
                       |
              Runtime Provider (Mock/Hermes)
                       |
        direct intent or per-Run Nico MCP socket
                       |
      Tool Gateway -> ToolCall/RunStep/Event/Audit
          |        |         |          |
      workspace   HTTPS   readonly DB   Sandbox Runner -> one-shot container
```

PostgreSQL 仍是权威状态；Runtime 不接收 ORM；Worker 不挂 Docker socket；Runner 不持有数据库/模型 Secret；Hermes native tools 全部关闭。

## 13. 下一阶段入口

Goal F 开始前读取本 Handoff、`docs/tool-gateway.md`、`docs/runtime.md`、ADR-0002/0005/0006/0008/0009、Goal Status 和 Feature Matrix。Goal F 应从终态 Run/RunStep/ToolCall/Trajectory 生成带来源的 MemoryCandidate/SkillCandidate，再实现四类 Memory、scope、pgvector 检索、Skill/SkillVersion、验证、审批、发布、canary 与回滚。

## 14. 下一阶段禁止重复实现的内容

- 不重建 Tool Registry、Gateway、ToolCall、文件/HTTP/DB/Python Executor 或 MCP broker；
- Memory/Skill 服务不能直接执行工具、调用 Hermes、读取跨租户轨迹或绕过 TenantContext/RLS；
- 不把未经验证的轨迹摘要直接发布为 Memory/Skill；
- 不提前实现 Team/Workflow、Plugin、量化交易、正式认证或 Web Console；
- 不让向量相似度越过 Tenant/Project/Agent/Team scope。

## 15. 建议下一步任务

1. 冻结 Goal F Memory/Skill schema、scope、来源链和候选状态机；
2. 先实现 tenant-safe Memory/SkillVersion 迁移、RLS 与不可变发布/回滚；
3. 实现 deterministic chunk/embed/retrieve 接口和 pgvector 过滤，先测跨租户与 scope 拒绝；
4. 从 Goal E 的规范轨迹构造候选，加入 evaluator、Approval、canary 和回滚证据；
5. 建立 Goal F 单元、真实 pgvector 集成与 Compose E2E，不使用 Mock 冒充语义检索质量。
