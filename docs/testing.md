# Testing and acceptance

## Recommended commands

| Scope | Command | Coverage |
| --- | --- | --- |
| Standard local gate | `scripts/test.sh` | Ruff check/format, backend unit tests, frontend tests, TypeScript, and production build |
| Installer/Release contracts | `scripts/test-install.sh` | 参数、Secret 保留、校验和、bundle allowlist、安全卸载、Compose Runtime 互斥及 CI trigger/权限 |
| Provider onboarding E2E | `scripts/e2e-provider-onboarding.sh` | 十个预设、三种协议、setup/chat、激活、回滚、维护门与 canary 泄漏扫描 |
| Offline Web tools E2E | `scripts/e2e-web-tools.sh` | 隔离数据库中的 configure、probe、AgentVersion、approval、Search、Fetch 与 citation 全链路 |
| Local Release rehearsal | `make release && make install` | 构建本地版本化镜像和真实 Release 资产，再通过正式安装器启动完整服务栈 |
| Backend unit tests | `.venv/bin/pytest backend/tests/unit` | Domain state, Runtime, tools, tenant isolation contracts, Memory, and Skill lifecycle |
| Frontend tests | `npm --prefix frontend test` | Health states plus Run Inspector deep link, ordering, loading/empty/error/partial/cancelled/redacted and hostile-text behavior |
| Real dependencies | `scripts/test-integration.sh` | Alembic replay, PostgreSQL RLS, Worker claims, Runtime/Tool persistence, and growth invariants |
| Compose stack | `scripts/e2e.sh` | Images, dependency health, API/OpenAPI, Console, Worker, and bucket initialization |
| Goal G native runtime | `scripts/e2e-goal-g.sh` | OpenAI-compatible fake model, default `nico_native`, streaming, ModelCall/ContextSnapshot/Event persistence, and zero-secret scan |
| Goal G full gate | `NICO_EVIDENCE_DIR=artifacts/goals/goal-g/<UTC> scripts/verify-goal-g.sh` | Source, migrations, all prior E2E, native E2E, and evidence manifest |
| Goal H ReAct recovery | `scripts/e2e-goal-h.sh` | Multi-round Tool Gateway loop, real Worker SIGKILL, lease takeover, idempotent side-effect reuse, Python sandbox, and contiguous events |
| Goal H full gate | `NICO_EVIDENCE_DIR=artifacts/goals/goal-h/<UTC> scripts/verify-goal-h.sh` | Full source/migration regression, Goal G Direct regression, Goal H fault injection, credential scan, and evidence manifest |
| Goal I planning E2E | `scripts/e2e-goal-i.sh` | Two Plan revisions, failed validation, constrained Reflection/Replan, deterministic Completion, separately billed judge, and read API |
| Goal I full gate | `NICO_EVIDENCE_DIR=artifacts/goals/goal-i/<UTC> scripts/verify-goal-i.sh` | Full source/migration regression, base Compose E2E, Goal I planning E2E, credential scan, and evidence manifest |
| Goal J multi-Agent recovery | `scripts/e2e-goal-j.sh` | Two parallel Child Runs, Parent suspension/wakeup, private shared Artifacts, Worker SIGKILL and replacement recovery |
| Goal J full gate | `NICO_EVIDENCE_DIR=artifacts/goals/goal-j/<UTC> scripts/verify-goal-j.sh` | Full source/migration regression, base Compose E2E, dynamic coordination fault injection, credential scan and evidence manifest |
| Goal K runtime knowledge | `scripts/e2e-goal-k.sh` | Governed publication, Tenant ∩ AgentVersion recall, exact Context refs, ModelCall consumption/effect facts and downstream GrowthSource |
| Goal K full gate | `NICO_EVIDENCE_DIR=artifacts/goals/goal-k/<UTC> scripts/verify-goal-k.sh` | Full source/migration regression, base Compose E2E, published Memory/Skill runtime acceptance, credential scan and evidence manifest |
| Goal L Hermes contract | `scripts/e2e-goal-l.sh` | Default-disabled failure without fallback, then explicit fake/local Hermes protocol-v2 success and compatibility facts |
| Goal L full gate | `NICO_EVIDENCE_DIR=artifacts/goals/goal-l/<UTC> scripts/verify-goal-l.sh` | Docs/Markdown/Compose checks, full suites, Goal G-L behavior regression, optional Adapter contract and evidence manifest |
| CLI Goal C chat E2E | `scripts/e2e-cli-goal-c.sh` | Two durable turns, continue/resume/history, SSE, JSON/no-ANSI and real SIGINT server cancellation |
| CLI Goal C full gate | `NICO_EVIDENCE_DIR=artifacts/goals/cli-goal-c/<UTC> scripts/verify-cli-goal-c.sh` | Source/docs/migration checks, all suites, CLI-B regression and CLI-C Compose E2E |
| CLI Goal D full gate | `NICO_EVIDENCE_DIR=artifacts/goals/cli-goal-d/<UTC> scripts/verify-cli-goal-d.sh` | All suites, migration roundtrip, CLI-B/C regression, exec/detach/watch, PTY slash and terminal-cat E2E |
| CLI Goal E context E2E | `scripts/e2e-cli-goal-e.sh` | Real staged bytes, Turn-owned Artifact, download, compact Run, summary and ContextSnapshot facts |
| CLI Goal E full gate | `NICO_EVIDENCE_DIR=artifacts/goals/cli-goal-e/<UTC> scripts/verify-cli-goal-e.sh` | All suites, migration roundtrip, CLI-B/C/D regression and Goal E security/E2E evidence |
| CLI Goal F approval E2E | `scripts/e2e-cli-goal-f.sh` | Non-interactive disconnect at ApprovalRequested, PTY resume, once/run decisions, two sensitive tools, audit and exactly-once execution |
| CLI Goal F full gate | `NICO_EVIDENCE_DIR=artifacts/goals/cli-goal-f/<UTC> scripts/verify-cli-goal-f.sh` | All suites, migration roundtrip, CLI-B–E regression, approval recovery/expiry and Goal F PTY evidence |
| CLI session controls E2E | `scripts/e2e-cli-session-controls.sh` | 异步 composer、持久 FIFO、双 Worker 串行、current/next 权限 footer、失败暂停、retry/resume 和隐私 canary |
| CLI session controls full gate | `NICO_EVIDENCE_DIR=artifacts/goals/cli-session-controls/<UTC> scripts/verify-cli-session-controls.sh` | 静态/单元/迁移/集成、CLI-C–F 回归及新 PTY session-controls 证据 |
| User Demo | `scripts/demo.sh` | Published AgentVersion through Task/Run, Worker, Mock Runtime, result, Events, and query URLs |

Integration tests are skipped by ordinary `pytest`. The integration script starts
or checks the required services and sets `RUN_INTEGRATION=1`; a skipped integration
test is not evidence of a passing integration suite.

The Web tools E2E is fully offline. Its deterministic fake Web fixture covers SearXNG JSON,
HTML, redirects, private targets, 429/5xx and slow responses; no live Provider credential or
Internet access is required. A live Brave smoke test is optional and must be explicitly
credential-gated. CI must never print its query or key. Unit benchmarks keep Search adapter
platform processing p95 below 100 ms and near-limit HTML extraction p95 below 250 ms; Provider
and remote network wait are intentionally excluded.

GitHub 的 `Test` workflow 在面向 `main` 的 Pull Request 和进入 `main` 的
push 上执行 `scripts/test.sh`、`scripts/test-integration.sh`、
`scripts/test-install.sh`、确定性的 Provider onboarding E2E 与离线 Web tools E2E，并以
`workflow_call` 暴露同一测试门。`Release` workflow 只监听 `v*` Tag；它先校验
annotated Tag、`main` 归属及 Python 包版本，再复用测试门，随后才获得
GHCR/GitHub Release 所需的写权限。成功发布包含三个多架构镜像、独立 CLI
wheel、安装器、bundle、镜像摘要和校验和；普通 merge 不发布这些内容。

## Security-sensitive coverage

The automated suite includes checks for:

- default-deny tool policy and Tenant/AgentVersion policy intersection;
- cross-tenant access and PostgreSQL RLS enforcement;
- Schema validation, Secret redaction, and terminal-record immutability;
- idempotency, retry, timeout, cancellation, heartbeat, and lease loss;
- path traversal, links, file races, and workspace quotas;
- SSRF, redirect, DNS, and database-read restrictions;
- one-shot Python containers with no network, a read-only root filesystem,
  non-root execution, resource limits, and cleanup;
- Memory/Skill provenance, independent approval, publication, canary selection,
  promotion, rollback, invalidation, and tenant isolation.
- model endpoint SSRF/DNS rebinding defenses, HTTPS policy, bounded streaming,
  Secret redaction, capability gates, retry boundaries, and hard/soft rate limits;
- ModelCall/ContextSnapshot same-tenant and same-Run integrity, terminal
  immutability, RuntimeSession loop state, and resumable SSE event sequence.
- ReAct budgets, exact tool versions, malformed Tool Call failure, pre-action and
  post-observation checkpoints, model replay relation, lease fencing, and
  idempotent ToolCall reuse after Worker failure.
- Plan DAG/cycle/budget validation, immutable revisions, failed step projection,
  constrained Reflection/Replan, deterministic Task acceptance, separately billed
  completion judge, schema-v3 recovery, real Tool Gateway execution with a
  pre-action checkpoint and parent trace, and Plan/Step/Evaluation tenant isolation.
- Coordination depth/count/parallelism/cycle/duplicate guards, concurrent budget
  reservation, permission/Secret-reference narrowing, Parent lease release,
  terminal Child wakeup, missed-notification reconciliation and tree cancellation.
- Private content-addressed Artifact upload/read, Child→Parent sharing, sibling
  denial, hash/size tamper detection, terminal immutability, upload bounds,
  anonymous-access rejection and temporary-object cleanup.
- Runtime Memory/Skill policy intersection, candidate/draft exclusion, exact
  version/hash freezing, untrusted context rendering, Child policy narrowing,
  Context/ModelCall consumption counts, terminal effects and Growth propagation.
- Provider implementation/capability/compatibility parity, explicit Hermes enable,
  pinned version, fail-closed resolution, historical session resume and legacy
  resolver deprecation telemetry.
- Run Inspector fixed information order, deep links, keyboard submission,
  accessible labels, partial/cancelled states, recursive redaction and hostile
  HTML rendered as text.

Goal G hermetic acceptance proves real HTTP/SSE inference against the included
OpenAI-compatible fake model without a Hermes binary. It does not prove an
external provider, model quality, profitability, or production readiness. A
credentialed operator endpoint is still required before Goal G can be marked
`Verified`; credentials must be supplied through an `env:NICO_MODEL_SECRET_*`
reference and must not appear in commands or evidence.

Goal H hermetic acceptance additionally kills a real Compose Worker after a
successful `file.write`, waits for lease expiry, starts a distinct replacement,
and proves that the cached write is not executed again before the Run continues
through the Python sandbox and final model response. A credentialed real-model
tool loop is still required before Goal H can be marked `Verified`.

Goal I hermetic acceptance deliberately makes revision 1 fail deterministic step
validation, persists Reflection and revision 2, then runs deterministic Completion
before a separate model judge. It proves orchestration and accounting semantics,
not external model quality; a credentialed real-model planning Run is still
required before Goal I can be marked `Verified`.

Goal J hermetic acceptance creates two Child Runs in one delegation round, kills
the real Compose Worker while both Child model calls are in flight, and proves
lease-expiry takeover, Child checkpoint recovery, Artifact exchange, Parent
wakeup and final aggregation. It proves orchestration and isolation semantics,
not the quality of an external model; credentialed acceptance remains separate.

Goal K hermetic acceptance uses the public governance APIs to generate, evaluate,
independently approve and publish one Memory and one Skill, then proves a later
Run consumes exactly those published versions and exposes only redacted usage/effect
facts. It proves runtime integration and provenance, not knowledge quality or
profitability; credentialed external-model acceptance remains separate.

Goal L hermetic acceptance first proves that an explicit Hermes AgentVersion fails
with `RUNTIME_PROVIDER_NOT_FOUND` on the default stack and never falls back to
Native. It then starts only the optional contract Worker and completes the same
Run contract through the fake/local Hermes `0.18.2` CLI. This proves adapter and
deployment behavior, not credentialed Hermes inference or model quality.

## Manual acceptance

For a release candidate, also verify:

1. `scripts/dev.sh --detach` reaches healthy API and Console states.
2. `/api/v1/health/ready` reports PostgreSQL, Redis, and MinIO ready.
3. `scripts/demo.sh` completes twice and reuses its stable Demo resources.
4. Redis failure produces a degraded readiness response and recovers after Redis
   returns.
5. `scripts/cleanup.sh` preserves data volumes, while
   `scripts/cleanup.sh --volumes` removes them only when explicitly requested.
6. The Console has no browser-console errors or horizontal overflow at desktop
   and mobile widths.

Historical acceptance logs are stored under `artifacts/goals/`. Treat them as
point-in-time evidence: current changes still require the relevant tests above.

## Nico CLI

CLI 的配置、HTTP client、SSE 分片、chat、输出和 Typer 命令测试：

```bash
.venv/bin/pytest backend/tests/unit/test_cli_*.py
```

CLI Goal B 真实路径会运行一个新的确定性 Mock Run，并使用安装后的 `nico` 子进程验证 profile 权限、health、doctor、Project、AgentVersion、Task、Run、Runtime、Event、JSON、`NO_COLOR` 和缺租户错误：

```bash
scripts/e2e-cli-goal-b.sh
```

CLI Goal C 的真实路径会重建实际 API/Worker 镜像，以安装后的 `nico` 子进程创建两个 Turn，检查 `--continue`、`--resume --read-only`、history、SSE、JSON 无 ANSI，并以真实 SIGINT 验证服务端 Run/Turn 取消：

```bash
scripts/e2e-cli-goal-c.sh
```

CLI Goal D 的真实路径覆盖 attached exec、严格 JSON input、私有原子 output、detach/watch、Event cursor 续读、彩色 TTY 像素猫、无色非 TTY，以及交互式 `/help`、`/inspect` 的 Plan/Step/Tool/Artifact/usage 视图：

```bash
scripts/e2e-cli-goal-d.sh
```

CLI Goal E 使用真实 MinIO、PostgreSQL、API、Worker 和安装后的 CLI，验证暂存附件、Turn 物化、受控下载、summary Run/ModelCall、冻结上下文选择和 ContextSnapshot：

```bash
scripts/e2e-cli-goal-e.sh
```

CLI Goal F 使用真实 PostgreSQL、API、Worker、Sandbox Runner、fake model 和安装后的 CLI。它先以 JSON 非交互模式在第一条 `ApprovalRequested` 处安全断开，再用 `--resume` 进入 PTY，分别选择 once 和 run，验证 Run 挂起/唤醒、双工具零重复、Event/Audit、脱敏参数和无 Secret 泄漏：

```bash
scripts/e2e-cli-goal-f.sh
```

Session controls E2E 启动两个真实 Worker，在第一条 Mock Run 仍活动时通过 PTY 连续
提交两条后续消息并切换 Conversation 权限。它验证数据库 FIFO 无执行时间重叠、下一
Run 才冻结新权限、后续 ContextSnapshot 包含此前完成 Turn、CLI detach 不取消队列，
以及失败后 `/retry` 与 `/queue resume` 的显式恢复。终端证据同时扫描模型 delta、
Runtime 内部事件名和测试 canary：

```bash
scripts/e2e-cli-session-controls.sh
```

Session controls 总验收运行相关静态/单元门、完整真实基础设施集成、CLI-C/D/E/F
回归和新的持久队列 PTY E2E：

```bash
NICO_EVIDENCE_DIR=artifacts/goals/cli-session-controls/<UTC> \
  scripts/verify-cli-session-controls.sh
```
