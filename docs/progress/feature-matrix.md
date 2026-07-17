# 功能矩阵

最后更新：2026-07-17 UTC。`未实现` 表示尚未进入对应 Goal；`不适用` 表示该阶段不以测试占位冒充行为实现。

| 功能 | 设计完成 | 代码完成 | 单测完成 | 集成测试 | E2E | 文档 | 最终状态 |
| -- | ---- | ---- | ---- | ---- | --- | -- | ---- |
| 需求基线与分阶段治理 | 已设计 | 已实现验证脚本 | 不适用 | 不适用 | Goal A 验收已通过 | 已完成 | 已验证 |
| 多租户 Tenant/Project 边界 | 已设计 | TenantContext、复合外键、运行角色、FORCE RLS、Project CRUD 已实现 | 状态与 API 契约已覆盖 | Schema/RLS/双租户隔离已通过 | 双租户 HTTP 隔离已通过 | 架构、领域、API、ADR 已完成 | Goal C 已验证 |
| Agent 生命周期与 AgentVersion | 已设计 | 身份资源、不可变配置版本、发布指针已实现 | 全状态机已覆盖 | 创建/版本发布/持久化已通过 | 发布版本完整链路已通过 | 领域/API/状态机已完成 | Goal C 已验证 |
| Agent 克隆、归档、恢复、回滚 | 已设计 | 已实现，Draft 无引用资源支持物理删除 | 转换与 OpenAPI 契约已覆盖 | 克隆/归档/恢复/回滚/删除已通过 | 发布与读取已覆盖 | 生命周期策略已完成 | Goal C 已验证 |
| Task 与多次 Run | 已设计 | Task/Run 分离、递增 attempt、retry_of_run_id 已实现 | 状态机与 revision 已覆盖 | 多次 Run、取消/重试已通过 | Task/Run 生命周期已通过 | 领域/API/状态机已完成 | Goal C 已验证 |
| RunStep、ModelCall 与 ToolCall 轨迹 | 已设计 | Runtime 与 Tool intent 均映射 RunStep/Event/Audit；版本化 ToolCall 终态不可变；ModelCall 尚未实现 | 终态不可变、Schema、错误和轨迹已覆盖 | ToolCall 原子性、幂等、租约恢复与 RLS 已通过 | 4 工具 ToolCall 完整路径已通过 | Runtime/Tool 文档已完成 | Goal E ToolCall 已验证；ModelCall 未实现 |
| Run 取消、重试、超时与恢复 | 已设计 | 权威取消、租约领取/心跳/过期接管、checkpoint 恢复与迟到结果拒绝已实现 | Provider 取消与状态已覆盖 | 并发 claim、恢复、旧 token、取消/失败已通过 | Worker 自动完成路径已通过 | Runtime/状态机已完成 | Goal D 已验证 |
| Event 与 Audit | 已设计 | 追加式双记录、correlation ID、同事务写入已实现 | API/错误原子性已覆盖 | 序列、隔离、冲突回滚已通过 | Run Event/Audit 查询已通过 | 追加式策略已完成 | Goal C 已验证 |
| Runtime Provider Protocol | 已设计 | 8 方法、capability、冻结 DTO、规范事件与错误已实现 | contract/registry/序列已通过 | Worker 应用服务集成已通过 | Provider-neutral Worker 路径已通过 | Runtime 文档与 ADR 已完成 | Goal D 已验证 |
| MockRuntimeProvider | 已设计 | 确定性成功/失败/暂停/恢复/取消/checkpoint/trajectory 已实现 | 全状态与序列已通过 | 成功/失败/取消/崩溃恢复已通过 | Compose Worker 自动执行已通过 | 配置与限制已完成 | Goal D 已验证 |
| HermesRuntimeProvider | 已设计 | CLI Adapter、版本 fail-closed、resume/取消/脱敏导出、每 Run HOME 与只启用 Nico MCP 已实现 | 配置权限/清理/环境脱敏与本地 0.18.2 兼容已通过 | 真实 Hermes 0.18.2 MCP initialize/tools/list 已通过；无模型凭据 | Tool Gateway E2E 使用 Mock；不声称真实推理 | Runtime/Tool 与运行依赖已完成 | Goal E MCP 边界已验证；真实推理未执行 |
| Tool Registry 与权限交集 | 已设计 | 精确 name@version Registry、不可变 Definition、租户∩AgentVersion 冻结策略、默认拒绝与实现 Hash 校验已实现 | Registry/Hash/权限/Schema/Secret/幂等/重试/取消已覆盖 | PostgreSQL RLS、并发同键、租约恢复、跨租户已通过 | 4 个授权工具经唯一 Gateway 执行已通过 | ADR-0009 与 Tool 文档已完成 | Goal E 已验证 |
| 文件、HTTP、DB、报告工具 | 已设计 | Run 工作区原子文件/报告、绑定校验 IP 的 GET/HEAD、只读参数化数据库查询已实现 | 路径/链接/竞态/限额、SSRF/DNS/重定向、SQL/角色/输出已覆盖 | 真实 PostgreSQL 只读角色与 HTTP loopback 故障通过 | 文件读写与 JSON 报告 Compose 路径已通过 | 配置、风险和调用示例已完成 | Goal E 已验证 |
| Python 沙箱 | 已设计 | 独立认证 Runner、固定 digest、一次性非 root/无网络/只读根容器与 CPU/内存/PID/时间/输出限制已实现 | Runner 合同、认证、payload 与错误已覆盖 | 真实 Docker 隔离、超时、截断、清理已通过 | Gateway→Runner 返回 UID 65534 且零残留容器 | 部署与威胁边界已完成 | Goal E 已验证 |
| 四类 Memory 与作用域 | 四类型、tenant/project/agent/team scope 与生命周期已冻结 | 未实现；team scope 在 Goal G 前失败关闭 | 未实现 | 未实现 | 未实现 | Goal F 计划与 Memory/Skill 边界已完成 | F1 设计已冻结 |
| pgvector 语义检索与来源追踪 | scope-first 检索、确定性 chunk/embed、完整来源快照已冻结 | 未实现 | 未实现 | 未实现 | 未实现 | ADR-0010 与检索合同已完成 | F1 设计已冻结 |
| Skill 与不可变 SkillVersion | 稳定身份、不可变版本、结构化内容与 deployment 已冻结 | 未实现 | 未实现 | 未实现 | 未实现 | 状态机与边界已完成 | F1 设计已冻结 |
| Candidate、验证、审批、发布与回滚 | content-hash 绑定的 Candidate→Evaluation→Approval→发布/灰度/回滚已冻结 | 未实现 | 未实现 | 未实现 | 未实现 | ADR-0005/0010 与阶段计划已完成 | F1 设计已冻结 |
| Team、Role 与 Membership | 已设计 | 未实现 | 未实现 | 未实现 | 未实现 | 领域模型已完成 | 仅设计 |
| 委派、审核、退回与汇总 Workflow | 已设计 | 未实现 | 未实现 | 未实现 | 未实现 | 状态边界与路线图已完成 | 仅设计 |
| Plugin Manifest、发现、校验与启停 | 已设计 | 未实现 | 未实现 | 未实现 | 未实现 | 插件 ADR 已完成 | 仅设计 |
| Quant Team Plugin | 边界已设计 | 未实现 | 未实现 | 未实现 | 未实现 | 路线图已完成 | 未实现 |
| REST API 与 OpenAPI | 已设计 | 健康、控制面、RuntimeSession/Trajectory、ToolDefinition/ToolCall 查询已实现 | API/OpenAPI 与敏感字段排除已通过 | 租户隔离和 Tool 查询通过 | 核心 HTTP、Runtime、Tool 生命周期已通过 | `docs/api.md` | Goal E 查询范围已验证；认证/SSE/SDK 待后续 |
| SSE Event Stream | 已设计 | 未实现 | 未实现 | 未实现 | 未实现 | 补读语义已完成 | 仅设计 |
| Python SDK | 边界已设计 | 未实现 | 未实现 | 未实现 | 未实现 | SDK 约束已完成 | 未实现 |
| TypeScript SDK | 边界已设计 | 未实现 | 未实现 | 未实现 | 未实现 | SDK 约束已完成 | 未实现 |
| Web Console | 信息范围已设计 | 真实基础设施状态页已实现 | 4 项组件测试 | 经 Nginx/API 验证 | 桌面/移动与故障 E2E 已通过 | 开发与测试文档已完成 | Goal B 骨架已验证；业务 Console 未实现 |
| API Key/JWT 与 Secret 隔离 | 已设计 | Tool Secret 引用、执行时解析、参数/结果/日志脱敏已实现；正式 API Key/JWT 未实现 | Secret resolver/红action/错误边界已覆盖 | ToolCall/Event/Audit/轨迹无明文通过 | E2E 无 Runner/lease token 泄露 | Tool 安全边界已完成 | Goal E Tool Secret 已验证；正式认证待 Goal K |
| 配置、结构化日志与健康检查 | 已设计 | Pydantic Settings、JSON 日志、关联 ID、并发探针已实现 | 10 项相关单测 | 三类真实依赖已通过 | 健康/降级/恢复已通过 | API/开发文档已完成 | Goal B 已验证 |
| PostgreSQL/pgvector/Redis/MinIO | 已决策 | RuntimeSession、ToolDefinition/ToolCall、claimer、运行角色/FORCE RLS 已加入 Compose/控制面 | 探针/状态/持久化规则已完成 | 31 项真实依赖/控制面/Runtime/Tool 测试通过 | API、Worker 与 Tool Gateway 使用真实 PostgreSQL 已通过 | ADR、领域与 Tool 文档已完成 | Goal E 存储范围已验证 |
| Docker Compose 与一键脚本 | 已规划 | 9 服务拓扑含非 root Worker 状态卷初始化与独立 Runner；Goal E 验收脚本已实现 | Shell/Compose 静态校验通过 | 固定镜像、迁移、依赖和 Runner 健康已验证 | `verify-goal-e.sh` 与三个阶段 E2E 已通过 | 开发/测试文档已完成 | Goal E 已验证 |
| 全量单元、集成、故障与 E2E 测试 | 已规划 | Goal E 测试出口已实现 | 后端 138 + 前端 7 通过 | 真实依赖 31 项通过 | Goal C 核心、Goal D Runtime、Goal E Tool/Sandbox E2E 通过 | `docs/testing.md` | Goal E 已验证 |
