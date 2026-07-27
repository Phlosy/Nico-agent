# ADR：Run-scoped External Tool Provider Binding

- 状态：Accepted
- 日期：2026-07-27
- 决策范围：Nico Agent Tool Gateway、Run 控制面、Worker

## 背景

Nico 的 Tool Definition 描述稳定工具合同，现有 Tool Registry 将合同绑定到进程内
executor。平台需要让调用方为单个 Run 把某个精确 Tool 交给受信任的外部服务执行，同时
保持 AgentVersion 权限、审批、租户/项目隔离、幂等、取消、审计和 Runtime 恢复边界。

## 决策

### 1. Definition 与 Binding 分离

`ToolDefinition` 继续表示 `name@version`、输入/输出 Schema、权限、风险、默认 timeout 和
retry。新增 `ExternalToolProvider` 表示受控、已验证的服务；新增 `RunToolBinding` 表示
Run 内一个 exact Tool 的执行者和持久化预算计数；`Run.tool_binding_snapshot` 是创建时
冻结的不可变合同。

同一 Tool 不因 Provider 不同而创建新 ToolVersion。Provider capability 只能证明其实现
匹配已存在合同，不能重定义合同。

### 2. 受控 Registry

公开注册接受 endpoint identity 与 Secret reference，但不接受明文 Secret。注册阶段对 URL、
scheme、host、port、DNS/IP、TLS 与部署 allow policy 做 fail-closed 校验。`verify` 通过
DNS-pinned transport 调用 `/v1/health` 和 `/v1/capabilities`，只有 VERIFIED/ACTIVE Provider
可被新 Run 引用。

默认只允许公网 HTTPS。内部 Provider 需要部署级显式 host allowlist，以及独立的
`allow_http_loopback`/`allow_private` 开关；Run 创建者不能覆盖这些策略。redirect 默认为零。

### 3. 创建时冻结

控制面在创建 Run 的同一数据库事务中：

1. 锁定 Task 并取得 project；
2. 取得精确 published/superseded AgentVersion；
3. 从 Tenant + AgentVersion 生成有效 Tool allowlist；
4. 对每个请求绑定校验 exact `name@version`、Provider scope/status/expiry/protocol；
5. 校验 Provider capability 的输入/输出 schema digest；
6. 校验 timeout、retry、approval 与所有预算均可落实；
7. 先生成 Run id，再把 tenant/project/run/agent/version scope 写入 Snapshot；
8. 写 `Run.tool_binding_snapshot` 和每个 `RunToolBinding` 计数行；
9. 写不含 Secret 的 freeze Event/Audit。

Snapshot 不提供 PATCH。ORM 服务拒绝对已有 Snapshot 的变化；数据库 trigger 阻止绕过服务
更新。Provider 后续 disable/revoke 不改写历史 Snapshot，但会使下一次执行 fail closed。

### 4. Binding-first resolution

Worker 的解析顺序固定为：

```text
frozen AgentVersion allowlist
-> exact name/version
-> exact Run binding
-> binding digest/scope/expiry/lifecycle
-> persistent budgets
-> approval
-> credential resolution
-> Provider invocation
-> envelope/output validation
-> counters/events/outcome
```

若 exact external binding 存在，Gateway 只能使用 `ExternalToolExecutor`。连接、认证、协议或
Provider 业务失败均返回结构化错误，不查询同名本地 executor。若不存在 external binding，
现有本地 Registry 路径继续工作。

### 5. 协议与认证

公开协议为 `nico-tool-provider-v1`。v1 使用短期 HMAC request signature：

- v1 长期根密钥只接受 `env:NICO_TOOL_SECRET_*` 形式的 `credential_ref`，并在
  Worker 内解析；
- 每次请求签名包含 method、path、body digest、provider/binding/run/request identity、
  timestamp 和 nonce；
- Provider 接受窗口默认 60 秒，并持久化/缓存 nonce 防 replay；
- deadline 与完整 scope 同时在 signed body 中；
- signature 和 Authorization 值不持久化、不进入模型；
- revoke/disable 会阻止 Nico 发行新签名。

HMAC 不是用户 bearer token，也不能单凭“签名合法”授权。Provider 必须验证 signed body 内
tenant/project/run/tool/version/deadline/idempotency 与其 provisioned binding scope。对于独立
部署，可在保持协议 envelope 不变时升级为 mTLS/workload identity。

### 6. 幂等与恢复

Nico 为首次 ToolCall 生成并持久化：

- `tool_call_id`：Nico durable call id；
- `provider_request_id`：Provider 协议 request id；
- `idempotency_key`：Runtime 稳定 action key。

重试、lease recovery 与“Provider 已成功但 Nico 尚未提交结果”的恢复始终重用三者。
Provider 对相同 idempotency key + 相同 request digest 返回原结果；不同 digest 返回
`409 idempotency_conflict`。Nico 仍使用现有 ToolCall 唯一约束做本地第一道防线。

### 7. 预算、timeout 与审批

每个 binding 冻结并强制：

- `max_calls`；
- `max_total_duration_ms`；
- `max_single_call_duration_ms`；
- `max_retries`。

`RunToolBinding` 行在 dispatch/finish 事务中加锁，保证 Worker 重启不重置计数。有效单次
deadline 是 Run 剩余时间、Tool Definition 默认、binding override 和 Provider policy 上限
的最小值。

审批模式为 `always`、`never`、`inherit`：

- `always`：每次具体 ToolCall + arguments digest + attempt 都需 durable approval；
- `never`：只在 Tool Definition/tenant policy 允许免审批时有效，否则创建 Run 失败；
- `inherit`：复用现有冻结 approval policy。

默认不把一次批准升级成 Run-wide grant。只有显式的已有 run-scope policy 才可授权后续调用，
并仍受 exact Tool 与 binding 上限约束。

### 8. 取消与终态

Run cancel/timeout：

1. 权威数据库状态立即阻止新调用；
2. 找到有 active provider request 的 ToolCall；
3. 若 capability 支持 cancel，发送 bounded cancel；
4. 记录确认、失败或未知；
5. 无论远端结果如何，继续完成本地终止；
6. 未确认时写 `external_execution_may_continue=true`。

Run 完成、失败、取消或超时后，binding 进入 COMPLETED/REVOKED/EXPIRED 等终态并拒绝新调用。
历史 Snapshot、Event 和 Audit 保留，但 Secret value 从未保存。

## 数据模型

### ExternalToolProvider

- tenant/project scope、name、protocol、endpoint identity；
- `env:NICO_TOOL_SECRET_*` credential reference（不保存 Secret 值）；
- REGISTERED/VERIFIED/ACTIVE/DISABLED/EXPIRED/REVOKED；
- verified capability snapshot 与 digest；
- endpoint security policy、expiry、metadata、revision。

### Run.tool_binding_snapshot

JSONB v1，包含 exact Tool、schema digest、provider/protocol/endpoint identity、opaque credential
ref、tenant/project/run/agent/version、冻结 policy/budget/expiry 和 binding digest。API read 对
credential ref 再脱敏。

### RunToolBinding

每个 Run + Tool 唯一。保存 provider id、binding digest、生命周期、max budget、已用 call
count/total duration、active request 与 cancel 状态。它是计数和 lifecycle projection，不是
可编辑配置源。

### ToolCall 扩展

保存可空的 provider/binding id、provider request id、provider execution id、request digest、
deadline、trace id、Provider status 和可能残留外部执行标记。本地 ToolCall 保持这些字段为空。

## 错误模型

Provider 错误继承稳定 `ToolError` 合同，但携带结构化字段：

```text
source, category, code, retryable, run_id, tool_call_id,
provider_id, attempt, timestamp, cause
```

稳定 code：

```text
TOOL_PROVIDER_NOT_FOUND
TOOL_PROVIDER_DISABLED
TOOL_PROVIDER_EXPIRED
TOOL_PROVIDER_SCOPE_MISMATCH
TOOL_PROVIDER_PROTOCOL_MISMATCH
TOOL_PROVIDER_CAPABILITY_MISMATCH
TOOL_PROVIDER_SCHEMA_MISMATCH
TOOL_PROVIDER_AUTH_ERROR
TOOL_PROVIDER_PERMISSION_DENIED
TOOL_PROVIDER_CONNECTION_ERROR
TOOL_PROVIDER_TIMEOUT
TOOL_PROVIDER_RATE_LIMIT
TOOL_PROVIDER_PROTOCOL_ERROR
TOOL_PROVIDER_INVALID_RESPONSE
TOOL_PROVIDER_CANCEL_ERROR
TOOL_BINDING_NOT_FOUND
TOOL_BINDING_IMMUTABLE
TOOL_BINDING_BUDGET_EXCEEDED
TOOL_BINDING_VERSION_MISMATCH
```

HTTP status 只参与协议分类，不直接代表 Tool 成功。成功必须同时满足：2xx、合法 envelope、
request id 匹配、scope/tool identity 匹配、状态合法、时间字段合法及 Tool output schema 通过。

## 生命周期

```text
Provider:
REGISTERED -> VERIFIED -> ACTIVE -> DISABLED
                         -> EXPIRED
                         -> REVOKED

Binding:
CREATED -> FROZEN -> ACTIVE -> EXPIRING -> EXPIRED
                         \-> REVOKED
                         \-> COMPLETED
```

`revoke` 不可逆；`disable` 阻止新绑定和新调用，但保留管理恢复空间。expiry 由请求时检查驱动，
不依赖后台轮询才能保证安全。

## 事件

新增：

```text
tool.binding.resolved
tool.provider.requested
tool.provider.started
tool.provider.completed
tool.provider.failed
tool.provider.cancel.requested
tool.provider.cancelled
tool.provider.expired
tool.provider.protocol_error
```

payload 仅存 id/digest/status/duration/error code/trace/sequence，不存 URL query、credential、
签名、cookie 或 response headers。

## Threat Model

| 威胁 | 防护 | 残余风险与默认行为 |
|---|---|---|
| 恶意 Agent arguments | Definition input schema、大小限制、canonical digest；Provider 仍按不可信输入处理 | 业务语义攻击需 Provider 自身 sandbox/权限；Nico fail closed 于 schema 错误。 |
| 恶意/被攻陷 Provider | exact capability/schema、response envelope/output schema、大小/时间限制、no fallback | Provider 仍可能在授权 scope 内产生恶意副作用；需独立隔离和最小凭据。 |
| SSRF/metadata | Registry-only endpoint、DNS pin、IP 分类、host/port/TLS policy | 明确管理员 allow 的内部 host 仍是信任边界。 |
| Redirect | v1 默认禁用；若将来允许，每 hop 重做 scope、DNS/IP 与 origin 校验 | Provider 不得通过 30x 转移 Authorization。 |
| DNS rebinding | 验证和调用时重新解析全部地址并连接已校验 IP；TLS 保留 SNI | DNS 控制者可造成可用性攻击，不能突破地址 policy。 |
| Token/Secret 泄漏 | Secret ref at rest、Worker-only resolve、短期签名、递归脱敏、禁止 event/header 落盘 | 内存转储和被攻陷 Worker 不在 v1 完全防护范围；部署需进程隔离。 |
| 跨 Tenant/Project/Run | DB composite FK/RLS、snapshot scope、signed body、Provider scope validation | Provider 若不实现 scope validation 会 verify 失败；运行时仍校验响应 identity。 |
| Replay | timestamp window、nonce、stable request/idempotency identity | Provider nonce store 丢失时依赖 idempotency store继续去重。 |
| Response tampering | TLS、request id/scope/tool identity、strict JSON/envelope/output schema | v1 不对 response 单独签名；TLS 终止点属于信任边界。 |
| Tool schema spoofing | canonical input/output digests在 capability verify 与 Run freeze 双重校验 | Provider 更新 capability 后只影响新 Run；旧 Run fail closed。 |
| 长时间不返回/超大响应 | 最小 deadline、connect/read timeout、streamed byte cap、bounded cancel | Provider 侧副作用可能继续，记录 residual flag。 |
| 伪造 Tool identity | signed exact name/version/schema/binding digest；响应必须回显并匹配 request | 不匹配归类 protocol error，不交给模型。 |
| cancel 后继续副作用 | cancel request、confirmation、binding terminal state、residual audit | 分布式取消不能保证回滚；v1 明确记录“可能继续”。 |
| Provider rate-limit/500 | 结构化 retryable 分类、冻结 retry budget、same idempotency key | 重试耗尽后失败，不回退本地。 |
| 日志注入 | 外部 message 长度限制、控制字符清洗、Secret redaction | 运维侧第三方 proxy 日志须独立配置。 |

## 被否决方案

1. **Run Create 直接提交 URL**：允许 SSRF、DNS rebinding 和无审计 endpoint 变化。
2. **外部轮询 ToolCall 后回写结果**：绕过 Worker lease、Secret、approval、timeout 与统一状态机。
3. **Provider 作为 ToolVersion**：把执行部署细节污染稳定合同并造成版本爆炸。
4. **Provider 失败时本地回退**：改变副作用位置与安全边界，违反可重复性。
5. **仅在 RuntimeSession 首次启动时冻结**：创建与执行间存在配置漂移窗口。
6. **把 credential 写入 Snapshot**：会暴露给 API、事件、模型或备份。

## 兼容性与迁移

没有 `tool_bindings` 的 Run 行为保持不变。新列可空或有空对象默认值；现有本地 ToolCall 不受
影响。迁移先添加 Provider/Binding/ToolCall 字段和约束，再发布 binding-aware Gateway。

## 已知限制

- v1 的 Secret backend 仍可先使用受限 env refs；vault/KMS 和自动根密钥 rotation 后续实现。
- HMAC response 不单独签名，依赖 TLS 与严格 identity validation。
- 分布式 cancel 不能撤回已经发生的外部副作用。
- 内部 HTTP Provider 只用于显式部署策略和本地 E2E；生产默认公网 HTTPS。

## 可逆性

高。Provider client、认证机制和 Secret backend 可替换；稳定的 Definition、Snapshot、binding
digest、ToolCall identity 和公开 protocol envelope 保持不变。
