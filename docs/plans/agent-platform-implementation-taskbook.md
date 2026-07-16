# 通用成长型多 Agent 服务平台实施任务书

你需要在当前代码仓库中设计并实现一套通用的、可扩展的、多租户成长型 Agent 服务平台。

该平台不是专门面向量化领域，而是作为通用 Agent 基础设施存在。后续可以通过领域插件，在平台上构建量化研究团队、软件开发团队、科研团队、卫星设计团队、运维团队等不同类型的多 Agent 团队。

量化团队将作为第一个验证插件，但不得将量化业务逻辑直接耦合到 Agent 核心中。

本项目需要采用分阶段 Goal 模式推进。不要尝试在一次上下文中完成全部工作。每个阶段必须形成完整、可运行、可测试、可验收、可接续的阶段成果。

---

# 一、项目总体目标

建设一套具备以下能力的通用 Agent 服务：

1. Agent 可以作为持久化资源被创建、修改、查询、克隆、归档和删除；
2. 每个 Agent 拥有独立身份、角色、目标、模型、工具权限、记忆、技能和运行配置；
3. Agent 可以接收结构化任务，而不仅是接收聊天消息；
4. 每次任务执行均形成可持久化、可恢复、可审计的 Run；
5. 支持多个 Agent 根据角色组成团队；
6. 支持任务委派、结果提交、审核、退回、否决和汇总；
7. 支持长期记忆、会话记忆、经验记忆和程序性技能；
8. 支持从任务轨迹中生成候选记忆和候选技能；
9. 任何自动成长都必须可验证、可审批、可回滚；
10. 支持通过插件注册领域角色、工具、技能、工作流、评价器和知识；
11. 支持接入 Hermes Agent，但系统架构不得永久绑定 Hermes；
12. 支持未来替换或增加其他 Agent Runtime；
13. 支持通过 REST API、事件流和 SDK 被外部业务系统调用；
14. 支持全过程追溯，包括模型输入、工具调用、输出、错误、成本和评价；
15. 最终提供完整的部署、运行、测试、演示和验收脚本。

---

# 二、核心架构原则

必须遵守以下架构原则。

## 2.1 通用平台与领域业务分离

Agent 平台只负责：

* Agent 生命周期；
* 任务与执行；
* Agent Runtime；
* 记忆与技能；
* 工具注册与调用；
* 团队编排；
* 插件加载；
* 权限与审计；
* 事件和产物管理；
* 模型 Provider；
* 沙箱和资源限制。

Agent 平台不得内置：

* MACD、RSI 等量化指标；
* 具体交易策略；
* 交易所业务逻辑；
* 因子研究逻辑；
* 实盘下单逻辑；
* 量化报告业务模板。

量化领域能力必须通过独立插件接入。

## 2.2 Agent 是持久化实体

Agent 不能只是一段 Prompt，也不能只存在于一次请求的内存中。

Agent 至少包含：

* 唯一 ID；
* 名称；
  -显示名称；
* 描述；
* 角色定义；
* 职责；
* 行为边界；
* 长期目标；
* 当前目标；
* 模型配置；
* 工具权限；
* 记忆策略；
* 技能策略；
* 插件引用；
* Token 和成本预算；
* 生命周期状态；
* 配置版本；
* 创建和更新时间。

## 2.3 任务与执行分离

Task 表示用户或团队希望完成的目标。

Run 表示某次具体执行。

同一个 Task 可以具有多次 Run，例如：

* 初次执行；
* 失败重试；
* 修改后重新执行；
* 使用新模型进行对比执行。

推荐关系：

```text
Task
  └── Run
        ├── Step
        ├── ToolCall
        ├── Artifact
        ├── MemoryOperation
        ├── Evaluation
        └── Event
```

## 2.4 Agent Runtime 可替换

定义统一的 AgentRuntimeProvider 接口。

至少应包含：

* create_session；
* run；
* pause；
* resume；
* cancel；
* get_status；
* stream_events；
* export_trajectory。

第一版可以实现 HermesRuntimeProvider。

Hermes 相关代码必须位于 Adapter 或 Provider 层中，禁止让业务层直接依赖 Hermes 内部类。

未来应能够扩展：

* HermesRuntimeProvider；
* CustomRuntimeProvider；
* LangGraphRuntimeProvider；
* RemoteRuntimeProvider。

## 2.5 所有成长行为必须受控

系统中的成长主要包括：

* 记忆增加；
* 记忆修订；
* 技能生成；
* 技能升级；
* 工作流优化；
* 工具选择策略调整；
* Agent 配置优化。

任何自动生成内容默认都只是 Candidate，不得直接覆盖正式版本。

建议流程：

```text
任务执行
→ 轨迹采集
→ 反思
→ 生成候选记忆或技能
→ 自动验证
→ 审批
→ 发布新版本
→ 灰度使用
→ 可回滚
```

---

# 三、推荐技术栈

第一版建议采用：

## 后端

* Python 3.11 或更高；
* FastAPI；
* Pydantic；
* SQLAlchemy；
* Alembic；
* asyncio。

## 前端

* React；
* TypeScript；
* Vite；
* TanStack Query；
* Zustand；
* React Flow；
* Monaco Editor；
* SSE 或 WebSocket。

## 数据与基础设施

* PostgreSQL；
* pgvector；
* Redis；
* MinIO；
* Docker Compose；
* 后续可增加 Kubernetes 部署。

第一版不强制引入 Kafka。

任务执行可以优先使用：

* Redis Queue；
* Dramatiq；
* Celery；
* 或自研的简化持久化 Worker。

需要在架构文档中解释最终选择。

---

# 四、核心领域模型

至少设计并实现以下核心对象：

1. Agent；
2. AgentVersion；
3. Team；
4. TeamMembership；
5. RoleDefinition；
6. Task；
7. Run；
8. RunStep；
9. ToolDefinition；
10. ToolCall；
11. Memory；
12. Skill；
13. SkillVersion；
14. Artifact；
15. Evaluation；
16. WorkflowDefinition；
17. Plugin；
18. Event；
19. Approval；
20. RuntimeProvider。

每个对象必须明确：

* 数据职责；
* 字段定义；
* 状态机；
* 对象之间的关系；
* 生命周期；
* 可变字段；
* 不可变字段；
* 删除或归档策略；
* 审计要求。

---

# 五、第一阶段必须实现的功能

第一阶段目标是完成一个真正可运行的通用 Agent 平台 MVP。

## 5.1 Agent 管理

支持：

* 创建 Agent；
* 查询 Agent；
* 列出 Agent；
* 更新 Agent；
* 克隆 Agent；
* 归档 Agent；
* 恢复 Agent；
* 删除 Agent；
* 查看 Agent 配置版本；
* 回滚 Agent 配置。

## 5.2 Task 与 Run

支持：

* 创建 Task；
* 为 Task 分配 Agent；
* 启动 Run；
* 查询 Run；
* 取消 Run；
* 失败重试；
* 设置最大执行步骤；
* 设置 Token 预算；
* 设置超时；
* 保存完整执行步骤；
* 保存模型调用；
* 保存工具调用；
* 保存错误；
* 保存产物；
* 保存执行成本。

## 5.3 多 Agent 团队

支持：

* 创建 Team；
* 为 Team 添加 Agent；
* 为 Agent 绑定 Role；
* 创建团队任务；
* 主管 Agent 委派子任务；
* 执行 Agent 提交结果；
* 审核 Agent 批准或退回；
* 主管 Agent 汇总结果。

第一版不要求实现无约束群聊。

优先采用显式工作流和状态机。

## 5.4 记忆

第一版至少支持：

* 工作记忆；
* 情景记忆；
* 语义记忆；
* 程序性记忆；
* Agent 私有记忆；
* Team 共享记忆；
* Project 记忆。

每条记忆必须包含：

* 来源 Run；
* 来源 Step；
* 作用域；
* 类型；
* 内容；
* 置信度；
* 状态；
* 创建者；
* 创建时间；
* 过期时间；
* 版本；
* 是否已审批。

需要实现：

* 写入；
* 查询；
* 语义检索；
* 更新；
* 失效；
* 删除；
* 来源追踪。

## 5.5 技能

Skill 不只是 Prompt。

每个 Skill 至少包含：

* 名称；
* 描述；
* 适用条件；
* 前置条件；
* 输入；
* 执行步骤；
* 所需工具；
* 输出结构；
* 验证规则；
* 已知失败模式；
* 来源任务；
* 版本；
* 成功率；
* 状态。

支持：

* 创建；
* 候选生成；
* 测试；
* 审批；
* 发布；
* 禁用；
* 回滚；
* 版本对比。

## 5.6 工具系统

设计统一的 Tool 接口。

工具至少包含：

* name；
* description；
* input_schema；
* output_schema；
* permission；
* timeout；
* retry_policy；
* isolation_policy；
* version。

工具执行必须记录：

* 调用者；
* 参数；
* 起止时间；
* 输出；
* 错误；
* 重试次数；
* 资源使用；
* 来源 Run 和 Step。

第一版至少实现：

* 文件读取工具；
* 文件写入工具；
* Python 沙箱工具；
* HTTP 只读请求工具；
* 简单数据库查询工具；
* 报告输出工具。

## 5.7 插件系统

插件至少能够注册：

* Role；
* Tool；
* Skill；
* Workflow；
* Evaluator；
* Knowledge；
* Schema；
* Permission；
* 可选 UI 扩展。

插件必须具有 manifest。

建议格式：

```yaml
apiVersion: agent.platform/v1alpha1
kind: DomainPlugin

metadata:
  name: example-plugin
  version: 0.1.0

spec:
  roles: []
  tools: []
  skills: []
  workflows: []
  evaluators: []
  permissions: {}
  runtime: {}
```

需要实现：

* 插件发现；
* 插件加载；
* 插件校验；
* 插件启用；
* 插件禁用；
* 插件版本查询；
* 插件兼容性检查。

---

# 六、量化团队验证插件

实现一个最小 quant-team-plugin，用于验证平台通用能力。

不得将量化代码写入核心模块。

第一版量化插件只需要三个角色：

1. Research Director；
2. Strategy Researcher；
3. Backtest Reviewer。

只需要五个工具：

1. 获取本地样例行情；
2. 计算简单指标；
3. 执行 Python；
4. 运行简单回测；
5. 生成研究报告。

只需要一个工作流：

```text
用户提交研究目标
→ 主管拆分任务
→ 研究员提出策略并运行回测
→ 审查员检查未来函数和结果完整性
→ 不通过则退回修改
→ 通过后主管输出最终报告
```

量化插件的作用只是验证：

* Agent 角色可注册；
* 工具可注册；
* 工作流可注册；
* 多 Agent 可以协作；
* 审核可以退回；
* 结果可以追溯；
* 领域逻辑与核心平台隔离。

禁止接入实盘交易。

---

# 七、API 与 SDK

平台至少提供以下 API：

## Agent API

```text
POST   /api/v1/agents
GET    /api/v1/agents
GET    /api/v1/agents/{id}
PATCH  /api/v1/agents/{id}
POST   /api/v1/agents/{id}/clone
POST   /api/v1/agents/{id}/archive
POST   /api/v1/agents/{id}/restore
DELETE /api/v1/agents/{id}
```

## Team API

```text
POST /api/v1/teams
GET  /api/v1/teams
GET  /api/v1/teams/{id}
POST /api/v1/teams/{id}/members
```

## Task 与 Run API

```text
POST /api/v1/tasks
GET  /api/v1/tasks/{id}
POST /api/v1/tasks/{id}/runs
GET  /api/v1/runs/{id}
POST /api/v1/runs/{id}/cancel
POST /api/v1/runs/{id}/retry
GET  /api/v1/runs/{id}/events
```

## Memory、Skill、Plugin 和 Artifact API

分别提供基础 CRUD、版本、查询和状态变更接口。

同时提供：

* Python SDK；
* TypeScript SDK；
* OpenAPI 文档；
* SSE 或 WebSocket 事件流。

SDK 底层必须通过 API 调用，禁止直接依赖服务内部数据库和实现类。

---

# 八、Web 管理界面

第一版 Web 至少提供：

1. Agent 列表；
2. Agent 创建与编辑；
3. Agent 详情；
4. Team 创建与成员关系；
5. Task 创建；
6. Run 实时执行过程；
7. Step 和 ToolCall 查看；
8. Memory 查看；
9. Skill 与 SkillVersion 查看；
10. Plugin 查看；
11. Artifact 查看；
12. 执行错误和审计日志查看。

Run 页面必须能看到：

```text
当前状态
执行 Agent
当前步骤
模型调用
工具调用
工具输入
工具输出
错误
Token 成本
执行产物
评价结果
```

---

# 九、状态机要求

必须明确设计状态机，不允许通过任意字符串表示状态。

至少定义：

## Agent 状态

```text
Draft
Ready
Running
Paused
Archived
Error
```

## Task 状态

```text
Created
Assigned
Running
WaitingForReview
RevisionRequired
Completed
Failed
Cancelled
```

## Run 状态

```text
Pending
Planning
Running
WaitingForTool
WaitingForApproval
Paused
Completed
Failed
Cancelled
TimedOut
```

## Skill 状态

```text
Candidate
Testing
Approved
Published
Deprecated
Disabled
```

每种状态转换都必须：

* 明确触发条件；
* 校验合法性；
* 记录事件；
* 支持审计。

---

# 十、可观测性与追溯要求

所有重要行为必须产生 Event。

至少包括：

* AgentCreated；
* AgentUpdated；
* TaskCreated；
* RunStarted；
* StepStarted；
* ModelCalled；
* ToolCalled；
* ToolSucceeded；
* ToolFailed；
* MemoryCreated；
* SkillCandidateCreated；
* ReviewRequested；
* ReviewRejected；
* RunCompleted；
* RunFailed。

每次 Run 必须可以导出完整执行轨迹。

建议支持 JSONL 和结构化 JSON。

每次运行必须关联：

* 代码版本；
* Agent 配置版本；
* Plugin 版本；
* Skill 版本；
* 模型名称；
* 模型参数；
* 工具版本；
* 输入数据版本。

---

# 十一、安全与权限要求

至少实现：

* API Key 或 JWT；
* Agent 工具白名单；
* 高风险工具审批；
* Secret 不进入 Prompt；
* 文件系统路径隔离；
* 网络访问白名单；
* 工具超时；
* 工具输出大小限制；
* Python 沙箱；
* 审计日志；
* Token 和成本限制。

第一版所有外部写操作和高风险工具默认关闭。

---

# 十二、测试要求

不得只完成接口和页面。

必须至少实现：

## 单元测试

覆盖：

* 状态转换；
* Agent CRUD；
* Task 和 Run；
* 插件注册；
* Memory；
* SkillVersion；
* Tool 权限；
* Runtime Provider。

## 集成测试

覆盖：

* 创建 Agent；
* 创建 Team；
* 安装量化插件；
* 创建任务；
* 启动 Run；
* Agent 调用工具；
* 生成 Artifact；
* 审核退回；
* 再次执行；
* 最终完成。

## 端到端测试

提供一键执行脚本，验证完整量化团队工作流。

## 故障测试

至少验证：

* 模型调用失败；
* 工具超时；
* Tool 返回错误；
* Worker 重启；
* Run 取消；
* Run 重试；
* 插件加载失败；
* 非法状态转换。

---

# 十三、项目交付物

最终仓库必须包含：

```text
README.md
docs/architecture.md
docs/domain-model.md
docs/state-machines.md
docs/plugin-system.md
docs/runtime-provider.md
docs/memory-and-skill.md
docs/api.md
docs/security.md
docs/testing.md
docs/roadmap.md
docs/decisions/
docs/handoffs/
scripts/bootstrap.sh
scripts/dev.sh
scripts/test.sh
scripts/e2e.sh
scripts/demo.sh
scripts/cleanup.sh
docker-compose.yml
.env.example
```

还必须提供：

* 数据库迁移；
* OpenAPI；
* 示例插件；
* Python SDK；
* TypeScript SDK；
* 完整测试；
* Demo 数据；
* 一键启动；
* 一键验收；
* 一键清理。

---

# 十四、分阶段 Goal 模式

必须按以下阶段推进，不得一次性混合实现。

## Goal A：仓库勘察与架构基线

完成：

* 检查现有仓库；
* 判断是否已有代码可复用；
* 明确技术栈；
* 输出总体架构；
* 输出领域模型；
* 输出 ADR；
* 创建阶段计划。

不得在架构不清晰时大规模写代码。

## Goal B：项目骨架与基础设施

完成：

* 后端工程；
* 前端工程；
* PostgreSQL；
* Redis；
* Alembic；
* 配置系统；
* 日志；
* 健康检查；
* Docker Compose；
* 一键启动。

## Goal C：Agent、Task、Run 核心模型

完成：

* 数据模型；
* CRUD；
* 状态机；
* Event；
* RunStep；
* 基础测试。

## Goal D：Runtime Provider

完成：

* AgentRuntimeProvider 接口；
* MockRuntimeProvider；
* HermesRuntimeProvider；
* 运行、取消、状态查询；
* 轨迹导出；
* 集成测试。

## Goal E：Tool 与 Sandbox

完成：

* Tool Registry；
* Tool 权限；
* Python 沙箱；
* ToolCall；
* 超时与重试；
* 审计。

## Goal F：Memory 与 Skill

完成：

* Memory 存储；
* 语义检索；
* Skill；
* SkillVersion；
* 候选生成；
* 审批和回滚。

## Goal G：Team 与 Workflow

完成：

* Team；
* Role；
* Membership；
* 工作流；
* 任务委派；
* 审核和退回。

## Goal H：Plugin System

完成：

* Plugin Manifest；
* 插件加载；
* 注册扩展；
* 版本和兼容性；
* 示例插件。

## Goal I：Quant Team Plugin

完成：

* 三个角色；
* 五个工具；
* 一个完整工作流；
* 研究报告；
* E2E 测试。

## Goal J：Web Console

完成：

* Agent；
* Team；
* Task；
* Run；
* Memory；
* Skill；
* Plugin；
* Artifact 页面。

## Goal K：可观测性、安全与完整验收

完成：

* 权限；
* 审计；
* 指标；
* 日志；
* 异常测试；
* 性能基线；
* 完整 Demo；
* 最终验收报告。

---

# 十五、每个 Goal 的强制交付格式

每完成一个 Goal，必须更新以下文件。

## 15.1 Goal 状态

维护：

```text
docs/progress/goal-status.md
```

格式必须包含：

| Goal | 状态 | 完成比例 | 验收结果 | 证据目录 | 当前阻塞 |
| ---- | -- | ---: | ---- | ---- | ---- |

状态只能是：

```text
Not Started
In Progress
Blocked
Implemented
Verified
```

只有测试和验收通过，才能标记 Verified。

## 15.2 功能矩阵

维护：

```text
docs/progress/feature-matrix.md
```

格式：

| 功能 | 设计完成 | 代码完成 | 单测完成 | 集成测试 | E2E | 文档 | 最终状态 |
| -- | ---- | ---- | ---- | ---- | --- | -- | ---- |

禁止只用“完成”描述一项功能。

必须区分：

* 已设计；
* 已编码；
* 已测试；
* 已验证；
* 部分实现；
* 占位实现；
* 未实现。

## 15.3 架构决策记录

所有重要决策写入：

```text
docs/decisions/ADR-XXXX-title.md
```

每个 ADR 必须包含：

* 背景；
* 问题；
* 候选方案；
* 最终选择；
* 选择原因；
* 代价；
* 后续影响；
* 可逆性。

## 15.4 阶段 Handoff

每个 Goal 结束时创建：

```text
docs/handoffs/YYYY-MM-DD-goal-x-handoff.md
```

必须包含：

1. 本阶段目标；
2. 实际完成内容；
3. 未完成内容；
4. 新增文件；
5. 修改文件；
6. 数据库变更；
7. API 变更；
8. 配置变更；
9. 测试命令；
10. 测试结果；
11. 已知问题；
12. 当前架构；
13. 下一阶段入口；
14. 下一阶段禁止重复实现的内容；
15. 建议下一步任务。

## 15.5 验收证据

每个 Goal 创建独立证据目录：

```text
artifacts/goals/goal-x/<timestamp>/
```

至少保存：

* 执行命令；
* 日志；
* 测试报告；
* API 输出；
* 页面截图；
* 示例数据；
* 错误记录；
* 版本信息；
* 验收总结。

---

# 十六、代码修改纪律

后续任何修改都必须遵守以下流程：

```text
读取最新 Handoff
→ 读取 Goal Status
→ 读取 Feature Matrix
→ 读取相关 ADR
→ 检查现有实现
→ 定义本次修改范围
→ 编写或更新测试
→ 修改实现
→ 执行回归测试
→ 更新文档和证据
→ 输出新的 Handoff
```

禁止：

* 未检查已有实现就重新创建模块；
* 只修改代码不更新 Feature Matrix；
* 只声明完成而不提供测试；
* 删除已有能力但不记录；
* 绕开 Runtime Provider 直接调用 Hermes；
* 将量化逻辑写入 Agent Core；
* 使用临时 Mock 冒充正式实现；
* 修改状态机但不更新文档和测试；
* 修改数据库结构但不提供迁移；
* 修改 API 但不更新 SDK 和 OpenAPI。

---

# 十七、占位实现标记

任何尚未真正实现的功能必须明确标记：

```text
PLACEHOLDER
MOCK
PARTIAL
NOT_IMPLEMENTED
```

并在 Feature Matrix 中如实记录。

禁止以以下方式伪装完成功能：

* 接口直接返回固定值；
* 前端只展示静态数据；
* 工具调用不执行真实逻辑；
* 状态机只修改字段但无约束；
* Memory 只写数据库但不支持检索；
* Skill 只有文本没有版本和验证；
* 插件只是读取 YAML 但没有真实注册。

---

# 十八、验收标准

项目只有同时满足以下条件，才可视为第一版完成：

1. 可以一键启动；
2. 可以创建多个独立 Agent；
3. 可以创建 Team；
4. 可以注册并启用插件；
5. 可以启动结构化 Task；
6. 可以形成持久化 Run；
7. Run 可以调用真实工具；
8. Run 可以被取消和重试；
9. Run 失败后可查看完整错误；
10. 多 Agent 可以完成委派、执行、审核和汇总；
11. 可以保存和检索 Memory；
12. 可以创建和发布 SkillVersion；
13. 可以查看完整事件和执行轨迹；
14. 量化插件可以完成一条 E2E 工作流；
15. 核心平台中不存在量化业务耦合；
16. 测试、文档和证据完整；
17. 所有脚本参数可配置；
18. 执行结果可复现；
19. 后续 Agent 可以只通过 Handoff 和文档继续开发；
20. 可以明确回答每项功能是未实现、部分实现、已编码还是已验证。

---

# 十九、首次执行要求

开始实现前，先完成以下操作：

1. 检查当前仓库结构；
2. 阅读已有 README、文档、代码和脚本；
3. 判断是否已包含 Hermes 或其他 Agent Framework；
4. 输出当前能力清单；
5. 输出缺口分析；
6. 输出目标架构；
7. 输出分阶段计划；
8. 创建 Goal Status；
9. 创建 Feature Matrix；
10. 创建第一批 ADR；
11. 再开始 Goal A。

不要直接跳过分析开始写大量代码。

在每个 Goal 中自行根据实际执行结果调整技术细节，但不得改变总体分层、可追溯要求和插件化方向。若必须改变核心架构，必须先新增 ADR，说明原因、影响和迁移方案。
