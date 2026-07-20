# Goal G Handoff：Nico Native Direct 与 Model Gateway

## 1. 本阶段目标

把 Nico Agent 从“主要编排外部 Runtime Adapter”迁移为可独立运行的通用 Agent Runtime：默认 Worker 使用 `nico_native`，通过独立 Model Gateway 完成 Direct 推理，并把上下文、模型调用、usage、事件和终态作为可审计事实持久化。

## 2. 实际完成内容

- Runtime Protocol v2：execution mode、ContextSeed、规范事件、terminal/suspended outcome、loop state 与 v1 terminal compatibility；
- 多租户 ModelEndpoint revision、ContextSnapshot、ModelCall 持久化、RLS、终态不可变与同 Run 复合完整性；
- provider-neutral Model Gateway、OpenAI-compatible streaming provider、Secret reference、capability gate、timeout/retry、Redis rate limit 和稳定错误；
- 模型 endpoint DNS/IP 校验与连接地址固定、Host/SNI 保留、redirect/proxy 禁用、响应大小限制及 Secret 脱敏；
- 默认 `nico_native` Direct loop：确定性上下文、单轮流式模型调用、delta 合并、usage/cost、checkpoint、取消和稳定终态；
- Worker 默认 Native，Hermes 与 Mock 均为显式 opt-in；Worker 镜像不依赖 Hermes；
- ModelEndpoint/ModelCall/ContextSnapshot REST API 与支持 `Last-Event-ID` 的 Run Event SSE；
- fake-model Compose profile、Goal G E2E 和总验收脚本。

## 3. 阶段状态与未完成内容

Goal G 状态为 `Implemented`，即 Code Complete，不是 `Verified`。Hermetic fake-model、完整单元/集成/Compose 回归均通过；由于当前没有操作者提供的真实模型 endpoint、model 与 `env:NICO_MODEL_SECRET_*` credential ref，计划规定的外部真实模型 Run 尚未执行。

Goal H 的 ReAct/Tool 多轮循环和崩溃恢复未提前实现。Native provider 不宣告 pause/resume capability；Direct 收到 Tool/Delegation action 会稳定失败关闭。

## 4. 新增文件

- `backend/migrations/versions/20260718_0010_native_model_runtime.py`；
- `backend/src/nico_agent/models/`；
- `backend/src/nico_agent/runtime/native/`；
- `backend/src/nico_agent/model_api.py`、`model_api_schemas.py`；
- `backend/src/nico_agent/testing/fake_model.py`；
- Goal G 单元与集成测试；
- `scripts/e2e-goal-g.sh`、`scripts/verify-goal-g.sh`；
- `docs/plans/2026-07-18-001-nico-native-runtime-architecture-migration-plan.md` 与本 Handoff。

## 5. 主要修改文件

主要修改 Runtime contracts/registry/service/executor、Worker、domain models/states、控制面与 API schema、配置、Compose、README、Runtime/API/架构/领域/安全/测试文档和进度矩阵。原有 Mock/Hermes/Tool/Growth 路径通过 compatibility 和回归测试保留。

## 6. 数据库变更

- AgentVersion 增加 runtime provider、execution mode、model endpoint/model 绑定和能力配置；
- RuntimeSession 增加 protocol/loop/checkpoint/model-call 状态；
- 新增 revisioned `model_endpoints`、immutable `context_snapshots`、terminal-guarded `model_calls`；
- ContextSnapshot、ModelCall 与 RuntimeSession last call 通过 tenant+run 复合键闭合；
- 三张新表启用 `FORCE ROW LEVEL SECURITY`；ModelEndpoint revision 创建使用 PostgreSQL advisory transaction lock；
- Alembic 已完成 head→base→head 全量重放。

## 7. API 变更

- `POST/GET /api/v1/model-endpoints` 与 `GET /api/v1/model-endpoints/{id}`；
- `GET /api/v1/model-calls` 与 `GET /api/v1/model-calls/{id}`；
- `GET /api/v1/context-snapshots/{id}`；
- `GET /api/v1/runs/{run_id}/events/stream`，支持 `Last-Event-ID` 或 `after_sequence`；
- 新 AgentVersion 缺省解析为 `nico_native/direct`；旧版本仍按已冻结字段或 legacy resolver 解析。

## 8. 配置变更

新增模型 endpoint 写入开关、HTTP timeout/size、trusted private hosts、HTTP trusted-host policy、Redis limiter 和模型 Secret reference 配置。生产环境默认不开放 endpoint 管理写入。Hermes 配置仍可用，但不再是默认运行前提。

## 9. 测试命令

```bash
scripts/test.sh
scripts/test-integration.sh
scripts/e2e.sh
scripts/e2e-goal-g.sh
NICO_EVIDENCE_DIR=artifacts/goals/goal-g/<UTC> scripts/verify-goal-g.sh
```

## 10. 测试结果

- Ruff 与格式检查：passed；
- 后端单元：197 passed；
- 前端：7 passed，TypeScript/Vite production build passed；
- 真实依赖集成：62 passed；
- Alembic head→base→head：passed；
- Goal C/D/E/F 回归 E2E 与 Goal G 无 Hermes fake-model E2E：passed；
- 真实外部模型：not run，缺少操作者 endpoint/model/credential ref。

最终证据目录：`artifacts/goals/goal-g/20260718T171138Z/`。

## 11. 已知限制

- Direct 只执行一次模型回合，不执行 Tool Call、delegation、plan 或 reflection；
- checkpoint 已版本化记录，但 Native 崩溃恢复和副作用重放保护属于 Goal H；
- SSE 当前以 PostgreSQL Event 补读为权威路径，Redis 低延迟通知不是本阶段验收项；
- cost 只有 endpoint revision 提供 pricing 且 usage 完整时才为 exact；
- 当前 Development Tenant Header 不是生产身份认证；
- fake model 只能证明协议/执行链路，不证明真实模型质量、盈利能力或 Provider 全兼容。

## 12. 当前运行架构

```text
Task / Run / immutable AgentVersion
              |
        Worker lease claim
              |
      Nico Native Direct loop
              |
   ContextSnapshot -> Model Gateway -> OpenAI-compatible endpoint
              |              |
       Runtime events    streaming/usage/error
              |
 RuntimeSession + ModelCall + Event + Run terminal result
              |
        REST query / resumable SSE
```

PostgreSQL/RLS 是事实与租户边界；Redis 用于分布式限流/通知；模型 Secret 只在请求前按逻辑引用解析。Hermes 是可选 Adapter，不是 Nico Native 的依赖。

## 13. 产品定位更正

本 Handoff 明确取代 Goal F Handoff 中“Goal G 实现 Team/Role/Membership/Workflow”的旧建议。根据后续产品定位确认，量化团队、科研团队等 Team 与业务 Workflow 的语义差异很大，应由对应领域系统基于 Nico 的 Agent/Task/Run/Tool/API 组合实现；Nico core 只提供通用单 Agent 与未来通用多 Agent Runtime 原语。`team` memory scope 仅作为兼容保留值并继续失败关闭。

## 14. 下一阶段禁止重复实现的内容

- 不重建 Model Gateway、ModelEndpoint/ModelCall/ContextSnapshot 或第二套模型 Secret/限流机制；
- 不让 Native loop、Hermes Adapter 或领域系统绕过 Tool Gateway；
- 不把 Team/Membership/量化 Workflow 重新塞回 Nico core；
- 不把 checkpoint 存在误写为恢复已完成，也不在缺少真实 endpoint 时把 Goal G 标为 Verified；
- 不改写已发布 AgentVersion、terminal ModelCall 或历史 ContextSnapshot。

## 15. 下一阶段入口

Goal H 按新迁移计划执行 U4/U5：先实现 ReAct 与 Tool Gateway 多轮闭环，再实现 pre-action/post-observation checkpoint、故障恢复和副作用重放保护。开始前读取本 Handoff、迁移计划、`docs/runtime.md`、`docs/tool-gateway.md`、`docs/state-machines.md`、Goal Status 和 Feature Matrix。真实模型凭据若在此期间就绪，应先补做 Goal G 外部模型验收，但这不扩大 Goal H 的功能范围。
