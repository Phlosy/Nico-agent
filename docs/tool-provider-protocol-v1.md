# Nico Tool Provider Protocol v1

协议标识：`nico-tool-provider-v1`

协议版本：`1`

## 1. 传输

- 生产默认 HTTPS，TLS certificate 和 hostname 必须验证。
- endpoint 必须来自已验证 Provider Registry。
- `Content-Type` 与 `Accept` 均为 `application/json`。
- UTF-8、有限 JSON、object 顶层；禁止 NaN/Infinity 和重复语义字段。
- redirect 默认禁止；response body 上限由 Nico 冻结 policy 约束。
- 时间为带 `Z`/offset 的 RFC 3339 UTC。
- id 是不透明字符串；实现不得从 id 推导权限。

## 2. Schema digest

Input/output schema digest 计算方式：

```text
"sha256:" + hex(
  SHA-256(
    UTF-8(
      JSON(schema, sort_keys=true, separators=(",", ":"), allow_nan=false)
    )
  )
)
```

对象 key 排序，数组顺序保留。Provider capability 与 Nico ToolDefinition 的两个 digest 必须
分别精确相等。

## 3. 认证

v1 profile 使用 HMAC-SHA256。Nico 发送：

```http
X-Nico-Protocol: nico-tool-provider-v1
X-Nico-Provider-Id: <provider_id>
X-Nico-Binding-Digest: <sha256:...>
X-Nico-Request-Id: <request_id>
X-Nico-Timestamp: <unix-seconds>
X-Nico-Nonce: <random-128-bit-hex>
X-Nico-Content-SHA256: <lowercase-hex>
Authorization: Nico-HMAC-SHA256 <lowercase-hex-signature>
```

签名输入是以下 UTF-8 文本，字段之间为单个 LF，末尾无额外 LF：

```text
METHOD
PATH_WITH_QUERY
CONTENT_SHA256
PROVIDER_ID
BINDING_DIGEST
REQUEST_ID
TIMESTAMP
NONCE
```

`GET` body digest 使用空 bytes 的 SHA-256。Provider 必须：

1. 使用 constant-time compare；
2. 拒绝超过配置窗口的 timestamp（默认 60 秒）；
3. 拒绝同一 credential scope 内重复 nonce；
4. 验证 provider/binding/request headers 与 body 相同；
5. 验证 body 中 tenant/project/run/tool/version/deadline/idempotency；
6. 不在日志记录 Authorization、签名或 Secret；
7. 被 disable/revoke/expire 后拒绝新请求。

合法 HMAC 只是认证，不是完整授权。scope 校验是必须步骤。

## 4. Health

```http
GET /v1/health
```

成功：`200`

```json
{
  "protocol_version": "1",
  "provider_id": "provider_01",
  "status": "healthy",
  "time": "2026-07-27T12:00:00Z"
}
```

`status` 只允许 `healthy`、`degraded`、`unhealthy`。只有 `healthy` 可通过首次 verify。

## 5. Capabilities

```http
GET /v1/capabilities
```

成功：`200`

```json
{
  "protocol_version": "1",
  "provider_id": "provider_01",
  "supported_tools": [
    {
      "name": "example.exec",
      "version": "1.0.0",
      "input_schema_digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "output_schema_digest": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    }
  ],
  "features": {
    "cancellation": true,
    "idempotency": true,
    "streaming": false,
    "healthcheck": true
  }
}
```

要求：

- `provider_id` 必须匹配 Registry id。
- 每个 `name@version` 唯一。
- v1 必须声明 `idempotency=true`、`healthcheck=true`、`streaming=false`。
- 未声明的 feature 视为 false。
- capabilities 成功 verify 后以 digest 保存；变化只用于新 Run。

## 6. Execute

```http
POST /v1/tool-calls
```

请求：

```json
{
  "protocol_version": "1",
  "provider_id": "provider_01",
  "binding_digest": "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "request_id": "req_01",
  "tool_call_id": "018f...",
  "idempotency_key": "runtime-action-7",
  "tenant_id": "018a...",
  "project_id": "018b...",
  "run_id": "018c...",
  "task_id": "018d...",
  "agent_id": "018e...",
  "agent_version_id": "018f...",
  "tool": {
    "name": "example.exec",
    "version": "1.0.0",
    "input_schema_digest": "sha256:aaaa...",
    "output_schema_digest": "sha256:bbbb..."
  },
  "arguments": {
    "command": "echo hello"
  },
  "deadline": "2026-07-27T12:02:00Z",
  "attempt": 1,
  "trace": {
    "trace_id": "trace_01",
    "parent_event_id": "event_01"
  }
}
```

约束：

- `attempt >= 1`，但 retry 不得改变 request id、tool_call id 或 idempotency key。
- deadline 必须在未来且不晚于 binding/run 的冻结上限。
- arguments 必须通过 exact Tool input schema。
- project/task 可按平台合同为 nullable；若存在必须匹配 provisioned scope。
- Provider 不得执行 request 中未 provision 的 run/tool/binding。

成功或业务失败响应均为结构化 envelope：

```json
{
  "protocol_version": "1",
  "provider_id": "provider_01",
  "binding_digest": "sha256:cccc...",
  "request_id": "req_01",
  "tool_call_id": "018f...",
  "tool": {
    "name": "example.exec",
    "version": "1.0.0"
  },
  "status": "succeeded",
  "result": {
    "stdout": "hello\n",
    "exit_code": 0
  },
  "error": null,
  "started_at": "2026-07-27T12:00:01Z",
  "finished_at": "2026-07-27T12:00:02Z",
  "provider_execution_id": "exec_01",
  "idempotency_replayed": false
}
```

`status` 只允许 `succeeded`、`failed`、`cancelled`、`timed_out`。`succeeded` 必须有 object
`result` 且 `error=null`；其他状态必须有：

```json
{
  "code": "PROVIDER_COMMAND_FAILED",
  "category": "execution",
  "retryable": false,
  "message": "command exited non-zero"
}
```

Nico 在交给 Runtime 前验证：

- HTTP 状态、JSON 与 envelope shape；
- provider/binding/request/tool_call/tool identity 回显；
- `started_at <= finished_at <=` 合理时钟窗口；
- error 分类；
- result bytes；
- exact Tool output schema。

任一失败均为 Provider protocol error，而不是 Tool success。

## 7. Idempotency

Provider 的去重 key 至少是：

```text
(provider_id, binding_digest, run_id, idempotency_key)
```

并保存 request digest 与完整终态响应。

- 相同 key + 相同 digest：返回原 response，`idempotency_replayed=true`。
- 相同 key + 不同 digest：返回 HTTP `409` 和 `idempotency_conflict`。
- 网络断开、Nico Worker 重启或本地结果提交丢失时，重发相同 identity。
- Provider 不得因 `attempt` 增长再次产生副作用。

## 8. Cancel

仅当 capability 声明 cancellation：

```http
POST /v1/tool-calls/{request_id}/cancel
```

请求：

```json
{
  "protocol_version": "1",
  "provider_id": "provider_01",
  "binding_digest": "sha256:cccc...",
  "request_id": "req_01",
  "tenant_id": "018a...",
  "project_id": "018b...",
  "run_id": "018c...",
  "reason": "run_cancelled",
  "deadline": "2026-07-27T12:00:10Z"
}
```

响应：

```json
{
  "protocol_version": "1",
  "provider_id": "provider_01",
  "request_id": "req_01",
  "status": "cancelled",
  "provider_execution_id": "exec_01",
  "acknowledged_at": "2026-07-27T12:00:05Z",
  "side_effects_may_continue": false
}
```

Cancel 自身必须幂等。`already_finished` 是合法确认状态。Provider 无法保证停止时必须返回
`side_effects_may_continue=true`；Nico 无论 cancel 是否成功都完成本地 Run 终止。

## 9. HTTP 与错误映射

| HTTP | 协议含义 | Nico 分类 | retryable |
|---|---|---|---|
| 200 | 合法 envelope，仍需读 `status` | 由 envelope 决定 | 由 envelope 决定 |
| 400/422 | 请求/协议拒绝 | `TOOL_PROVIDER_PROTOCOL_ERROR` | false |
| 401 | 认证失败 | `TOOL_PROVIDER_AUTH_ERROR` | false |
| 403 | scope/权限失败 | `TOOL_PROVIDER_PERMISSION_DENIED` | false |
| 404 | capability/request 不存在 | `TOOL_PROVIDER_CAPABILITY_MISMATCH` | false |
| 409 | idempotency conflict | `TOOL_PROVIDER_PROTOCOL_ERROR` | false |
| 429 | rate limit | `TOOL_PROVIDER_RATE_LIMIT` | true |
| 5xx | Provider unavailable | `TOOL_PROVIDER_CONNECTION_ERROR` | true |

Connection、TLS、DNS 与 read failures 由 typed transport exception 分类；不得主要依赖字符串。

错误对象不得包含 Secret、Authorization、cookie、完整 endpoint query 或未经长度限制的 Provider
message。

## 10. Provider conformance

Provider 要通过以下 contract tests 才能 VERIFIED：

- auth missing/invalid/stale/replay；
- tenant/project/run/tool/version/binding scope mismatch；
- capability exact schema digest；
- execute success/failure/timeout/malformed；
- stable idempotency replay/conflict；
- cancel idempotency；
- bounded response；
- no redirect；
- no Secret in response/error。

## 11. Versioning

`protocol_version` 必须精确为 `"1"`。Nico 不做“最新版本”推断。未来 v2 使用新 protocol
identifier或显式协商；v1 Run Snapshot 在其生命周期内不升级。
