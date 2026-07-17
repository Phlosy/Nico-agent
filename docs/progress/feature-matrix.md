# 功能矩阵

最后更新：2026-07-17 UTC。`未实现` 表示尚未进入对应 Goal；`不适用` 表示该阶段不以测试占位冒充行为实现。

| 功能 | 设计完成 | 代码完成 | 单测完成 | 集成测试 | E2E | 文档 | 最终状态 |
| -- | ---- | ---- | ---- | ---- | --- | -- | ---- |
| 需求基线与分阶段治理 | 已设计 | 已实现验证脚本 | 不适用 | 不适用 | Goal A 验收已通过 | 已完成 | 已验证 |
| 多租户 Tenant/Project 边界 | 已设计 | TenantContext、复合外键、运行角色、FORCE RLS、Project CRUD 已实现 | 状态与 API 契约已覆盖 | Schema/RLS/双租户隔离已通过 | 双租户 HTTP 隔离已通过 | 架构、领域、API、ADR 已完成 | Goal C 已验证 |
| Agent 生命周期与 AgentVersion | 已设计 | 身份资源、不可变配置版本、发布指针已实现 | 全状态机已覆盖 | 创建/版本发布/持久化已通过 | 发布版本完整链路已通过 | 领域/API/状态机已完成 | Goal C 已验证 |
| Agent 克隆、归档、恢复、回滚 | 已设计 | 已实现，Draft 无引用资源支持物理删除 | 转换与 OpenAPI 契约已覆盖 | 克隆/归档/恢复/回滚/删除已通过 | 发布与读取已覆盖 | 生命周期策略已完成 | Goal C 已验证 |
| Task 与多次 Run | 已设计 | Task/Run 分离、递增 attempt、retry_of_run_id 已实现 | 状态机与 revision 已覆盖 | 多次 Run、取消/重试已通过 | Task/Run 生命周期已通过 | 领域/API/状态机已完成 | Goal C 已验证 |
| RunStep、ModelCall 与 ToolCall 轨迹 | 已设计 | RunStep/Event 已实现；ModelCall/ToolCall 待 Goal D/E | RunStep 状态机已覆盖 | Step 与 Event 持久化已通过 | Step 完整转换已通过 | Goal C 边界已记录 | Goal C 部分实现，后续对象未实现 |
| Run 取消、重试、超时与恢复 | 已设计 | 取消、重试、TimedOut 状态已实现；租约字段已预留；领取/恢复待 Goal D | Run 终态和转换已覆盖 | 取消、重试、非法转换已通过 | 基本完成路径已通过 | 状态机与边界已完成 | Goal C 范围已验证；执行恢复未实现 |
| Event 与 Audit | 已设计 | 追加式双记录、correlation ID、同事务写入已实现 | API/错误原子性已覆盖 | 序列、隔离、冲突回滚已通过 | Run Event/Audit 查询已通过 | 追加式策略已完成 | Goal C 已验证 |
| Runtime Provider Protocol | 已设计 | 未实现 | 未实现 | 未实现 | 未实现 | ADR 与接口轮廓已完成 | 仅设计 |
| MockRuntimeProvider | 已设计 | 未实现 | 未实现 | 未实现 | 未实现 | 路线图已完成 | 未实现 |
| HermesRuntimeProvider | 已设计 | 未实现 | 未实现 | 未实现 | 未实现 | 隔离边界已完成 | 未实现 |
| Tool Registry 与权限交集 | 已设计 | 未实现 | 未实现 | 未实现 | 未实现 | 架构已完成 | 仅设计 |
| 文件、HTTP、DB、报告工具 | 已设计 | 未实现 | 未实现 | 未实现 | 未实现 | 安全边界已完成 | 仅设计 |
| Python 沙箱 | 已设计 | 未实现 | 未实现 | 未实现 | 未实现 | 资源与网络边界已完成 | 仅设计 |
| 四类 Memory 与作用域 | 已设计 | 未实现 | 未实现 | 未实现 | 未实现 | 领域模型已完成 | 仅设计 |
| pgvector 语义检索与来源追踪 | 已设计 | 未实现 | 未实现 | 未实现 | 未实现 | 存储决策已完成 | 仅设计 |
| Skill 与不可变 SkillVersion | 已设计 | 未实现 | 未实现 | 未实现 | 未实现 | 领域模型已完成 | 仅设计 |
| Candidate、验证、审批、发布与回滚 | 已设计 | 未实现 | 未实现 | 未实现 | 未实现 | 成长 ADR 已完成 | 仅设计 |
| Team、Role 与 Membership | 已设计 | 未实现 | 未实现 | 未实现 | 未实现 | 领域模型已完成 | 仅设计 |
| 委派、审核、退回与汇总 Workflow | 已设计 | 未实现 | 未实现 | 未实现 | 未实现 | 状态边界与路线图已完成 | 仅设计 |
| Plugin Manifest、发现、校验与启停 | 已设计 | 未实现 | 未实现 | 未实现 | 未实现 | 插件 ADR 已完成 | 仅设计 |
| Quant Team Plugin | 边界已设计 | 未实现 | 未实现 | 未实现 | 未实现 | 路线图已完成 | 未实现 |
| REST API 与 OpenAPI | 已设计 | 健康与 Goal C 控制面 API 已实现 | 8 项 API 单测 | 4 项控制面场景及基础设施通过 | 完整核心 HTTP 生命周期已通过 | `docs/api.md` | Goal C 控制面已验证；认证/SSE/SDK 待后续 |
| SSE Event Stream | 已设计 | 未实现 | 未实现 | 未实现 | 未实现 | 补读语义已完成 | 仅设计 |
| Python SDK | 边界已设计 | 未实现 | 未实现 | 未实现 | 未实现 | SDK 约束已完成 | 未实现 |
| TypeScript SDK | 边界已设计 | 未实现 | 未实现 | 未实现 | 未实现 | SDK 约束已完成 | 未实现 |
| Web Console | 信息范围已设计 | 真实基础设施状态页已实现 | 4 项组件测试 | 经 Nginx/API 验证 | 桌面/移动与故障 E2E 已通过 | 开发与测试文档已完成 | Goal B 骨架已验证；业务 Console 未实现 |
| API Key/JWT 与 Secret 隔离 | 已设计 | 未实现 | 未实现 | 未实现 | 未实现 | 多租户/工具边界已完成 | 仅设计 |
| 配置、结构化日志与健康检查 | 已设计 | Pydantic Settings、JSON 日志、关联 ID、并发探针已实现 | 10 项相关单测 | 三类真实依赖已通过 | 健康/降级/恢复已通过 | API/开发文档已完成 | Goal B 已验证 |
| PostgreSQL/pgvector/Redis/MinIO | 已决策 | Compose、扩展与 9 张 Goal C 业务表、运行角色/RLS 已实现 | 探针/状态规则已完成 | 10 项真实依赖/控制面测试通过 | 核心 API 使用真实 PostgreSQL 已通过 | ADR、领域与测试文档已完成 | Goal C 存储范围已验证 |
| Docker Compose 与一键脚本 | 已规划 | 7 服务拓扑及 Goal C 验收脚本已实现 | Shell/Compose 静态校验通过 | 镜像、迁移、依赖健康已验证 | `verify-goal-c.sh` 通过 | 开发/测试文档已完成 | Goal C 已验证 |
| 全量单元、集成、故障与 E2E 测试 | 已规划 | Goal C 测试出口已实现 | 后端 31 + 前端 7 通过 | 真实依赖 10 项通过 | Goal B 基础设施与 Goal C 核心 API E2E 通过 | `docs/testing.md` | Goal C 范围已验证；Runtime/Tool 故障待后续 Goal |
