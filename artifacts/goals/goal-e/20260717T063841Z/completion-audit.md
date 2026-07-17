# Goal E requirement-by-requirement completion audit

| Objective requirement | Authoritative implementation evidence | Verification evidence | Result |
| --- | --- | --- | --- |
| Platform-only Tool Gateway | `runtime/tools.py`, `runtime/executor.py`, `tools/gateway.py`; no direct execution REST API | `test_mock_runtime_executes_real_tools_only_through_gateway`; Goal E Compose E2E | Proven |
| Versioned ToolDefinition and immutable ToolCall | migrations `0004`/`0005`, Registry exact lookup, DB terminal triggers | `test_enabled_definition_and_terminal_call_are_database_immutable`, `test_tool_call_scope_and_idempotency_are_database_enforced` | Proven |
| Tenant and AgentVersion permission intersection | frozen `RuntimeSession.tool_policy_snapshot`, fail-closed policy composition | all `test_tool_policy.py`; Gateway denial integration; FORCE RLS isolation | Proven |
| Input/output Schema and limits | `ToolDefinitionSpec` contract validation before/after execution | `test_definition_is_canonical...`, output limit and Gateway invalid-input tests | Proven |
| Secret outside Prompt/Event/log/trajectory | strict `env:NICO_TOOL_SECRET_*` resolver and recursive persistence redaction | policy/Secret unit tests; `test_gateway_success_is_redacted_audited_and_idempotent`; E2E token assertions | Proven |
| Idempotency, timeout, retry, cancel and lease recovery | Run/tool lease checks, stable key+arguments Hash, declared retry codes, bounded timeout/cancel | Gateway retry/timeout/cancel/takeover tests; MCP restart stability; Worker recovery and late-result tests | Proven |
| Complete ToolCall/RunStep/Event/Audit trajectory | Gateway start/reject/finish transactions and Runtime tool events | Gateway integration; Runtime worker integration; E2E API artifacts | Proven |
| Tenant/Run file workspace and reports | dirfd/`O_NOFOLLOW`, atomic fsync/replace, quota/flock, report formats | 14 workspace tests including traversal/link/FIFO/concurrency/isolation; Goal E E2E | Proven |
| Safe GET/HEAD HTTP | allowlist, all-address IP checks, per-redirect reauthorization, pinned socket/TLS SNI, no proxy | 24 HTTP tests covering private/mixed/mapped DNS, redirects, limits and loopback dual gate | Proven |
| Read-only parameterized database | strict single SELECT/WITH parser, readonly transaction/role, timeout/row/output bounds | 21 DB unit tests and real PostgreSQL readonly-role integration | Proven |
| Isolated Python execution | authenticated independent Runner; fixed digest; non-root/network none/readonly root/cap drop/resource bounds/cleanup | 6 unit tests, real Docker integration, Compose UID 65534 result and empty residual-container artifact | Proven |
| Runtime intent and Hermes cannot bypass | `platform_tools` capability, per-Run MCP Unix broker, Hermes only `nico`, native toolsets disabled, sanitized env/home | MCP protocol/auth/restart tests, Hermes config unit test, real Hermes 0.18.2 discovery integration | Proven |
| Cross-tenant and persistence boundaries | composite FKs, `nico_runtime`, FORCE RLS, claim/lease revalidation | Tool persistence RLS test, Gateway tenant cases, Goal C isolation E2E | Proven |
| Complete runnable/testable/acceptable/continuable stage | Compose services/volumes, verifier, docs, API artifacts and Handoff | `verify.log`: 138 unit, 7 frontend, 31 integration, Goal C/D/E E2E, final PASS | Proven |

The first `20260717T063652Z` verification correctly failed on a Goal C/Worker race and is retained. Commit `7cd4416` isolated the control-plane-only test; the full verifier then passed from scratch in this directory. No objective requirement relies solely on intent, a skipped test, or a narrower mock success.
