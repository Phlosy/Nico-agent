# Terminal Provider Integration Readiness

Status: **READY WITH DECLARED CAPABILITY GAPS**

Audit date: 2026-07-27

Audited revisions:

- Nico Agent: `e81729a` (`feat/run-scoped-tool-provider-binding`)
- Nico Bench: `a6a932e` (`feat/phase-3-terminal-bench`)
- Nico Tool Provider protocol: `nico-tool-provider-v1`, version `1`
- Terminal tool: `terminal.exec@1.0.0`

This audit covers only the public integration required for the first real Nico
Terminal-Bench run. It does not authorize an internal Nico import, database
mutation, Tool-call polling executor, local Tool fallback, host shell, or Docker
socket access.

## Readiness matrix

| Requirement | Public evidence | Result |
|---|---|---|
| Register Provider | `POST /api/v1/external-tool-providers` | Ready |
| Verify Provider and freeze capabilities | `POST /api/v1/external-tool-providers/{provider_id}/verify` | Ready |
| Register exact Tool contract | `POST /api/v1/tool-definitions` | Ready |
| Bind exact Tool on Run creation | `POST /api/v1/tasks/{task_id}/runs` accepts `tool_bindings` | Ready |
| Immutable binding evidence | Run response exposes a redacted `tool_binding_snapshot` and `binding_digest` | Ready |
| Binding-first Worker resolution | `ExternalToolResolver` resolves the frozen exact Tool before the local registry | Ready |
| Provider transport | HMAC-authenticated, DNS-pinned HTTP with no redirects or proxy inheritance | Ready |
| Provider failure fallback | Exact external binding failures do not switch to a local Tool | Ready |
| Cancellation and idempotency | Protocol v1 and the Run-scoped binding policy support both | Ready |
| Agent/runtime/model freeze | Public AgentVersion and RuntimeSession resources expose the selected identities | Ready |
| Terminal environment authority | Existing Harbor bridge creates one run-bound `TerminalToolGateway` over the exact live environment | Ready |
| Official evaluation | Existing Phase 3.1 Harbor verifier remains the only task-success authority | Ready |

## Exact Tool contract

The integration uses one Tool and no aliases:

```text
name: terminal.exec
version: 1.0.0
permission: benchmark.terminal.exec
provider protocol: nico-tool-provider-v1
input schema digest: sha256:abe2fea625e66a0559523207ab2d3c1b672800dbd080897ff74993739e63fb22
output schema digest: sha256:155dba269217d9755760997831bfe42030c22fe2d5e53e426f4d75807c209c6e
```

The canonical schemas and their digests live in the Nico Bench provider
contract module. Digests use the algorithm in
`docs/tool-provider-protocol-v1.md`: UTF-8 JSON with sorted object keys and
compact separators, followed by SHA-256. Provider capabilities, the public
Tool Definition, and the Run binding snapshot must contain identical input and
output digests. Any mismatch fails Provider verification or Run creation.

The input schema accepts:

- a required non-empty `command`;
- an optional absolute `cwd`;
- an optional command timeout from 1 through 300 seconds;
- an optional bounded environment map with platform-safe, non-secret names;
- no unknown fields.

The output schema keeps command execution distinct from Provider transport
success. A command that runs and exits non-zero is a successful Provider
exchange with `status: "failed"`, its real exit code, stdout, and stderr. This
is required so Nico can inspect the command failure and recover. Provider
authentication, scope, timeout, cancellation, and protocol failures remain
non-success Provider envelopes.

## Public registration sequence

Nico Bench can perform the complete lifecycle without persistence access:

```text
start loopback Terminal Provider
  -> POST /api/v1/tool-definitions
  -> POST /api/v1/external-tool-providers
  -> set the returned provider_id on the running Provider
  -> POST /api/v1/external-tool-providers/{provider_id}/verify
  -> POST /api/v1/tasks
  -> POST /api/v1/tasks/{task_id}/runs with tool_bindings
  -> provision the returned immutable binding scope into the Provider
  -> observe the Run through public Run/Event/Trajectory resources
  -> POST /api/v1/external-tool-providers/{provider_id}/revoke
```

The credential is an operator-provisioned
`env:NICO_TOOL_SECRET_*` reference available to both the Nico API/Worker and
the colocated Provider. The secret value is never sent through a Nico public
resource or written to a benchmark artifact.

## Run creation and binding freeze

The real Run request uses:

```yaml
max_steps: 50
token_budget: 50000
timeout_seconds: 1800
budgets:
  max_tool_calls: 30
tool_bindings:
  - provider_id: <runtime registration result>
    tool:
      name: terminal.exec
      version: 1.0.0
    policy:
      timeout_seconds: 300
      max_calls: 30
      max_total_duration: 1800
      max_single_call_duration: 300
      retry:
        max_attempts: 1
      approval_mode: never
```

Run creation health-checks the active Provider before freezing the binding.
The response must contain exactly one redacted snapshot entry whose scope
matches tenant, project, Run, Task, Agent, and AgentVersion. Nico Bench rejects
the Run before Agent execution if the exact Tool identity, schema digests,
scope, or policy differs.

## Worker resolution

The audited runtime path is:

```text
NicoNativeRuntimeProvider
  -> ToolGateway
  -> ExternalToolResolver
  -> ToolProviderClient
  -> Terminal Provider HTTP listener
  -> TerminalToolGateway
  -> Harbor BaseEnvironment
  -> task container
```

The Provider endpoint can execute only through the private Gateway instance
created by Harbor's Agent bridge. The request cannot select a container,
environment, or host command runner. The Gateway verifies its immutable
benchmark/Task/environment/Trial/Agent-execution binding again before every
command.

## Provisioning race

The binding digest and Nico Run ID exist only after public Run creation, while
the Worker may claim that Run immediately. The Provider therefore authenticates
an early request, waits for a short bounded local provisioning event, and then
performs exact scope authorization. It never executes before provisioning.
This closes the Run-create/Worker-claim race without deriving authority from
the request and without polling Tool calls.

## Frozen benchmark identity

Each real task freezes and records:

- benchmark Run ID and Nico Run ID;
- `deepseek-v4-pro`, temperature `0`;
- the immutable AgentVersion content hash;
- Nico Native runtime provider/version/protocol;
- `terminal.exec@1.0.0` schema and binding digests;
- Terminal-Bench task revision and content digest;
- Harbor version and Trial ID;
- source and resolved image digests;
- container identity;
- official evaluator identity and reward.

## Capability gaps

1. **Hard maximum cost enforcement.** Nico records model-call cost when the
   Provider reports enough usage/pricing data, but Run Create has no hard
   `max_cost` enforcement field. The manifest freezes cost budgeting as enabled
   and artifacts report the gap; the integration does not claim that Nico can
   stop exactly at that limit.
2. **Hard token ceiling.** `token_budget` is mapped publicly, but the current
   Nico Runtime treats provider usage as post-response evidence. It cannot
   guarantee that an individual model response will not cross the remaining
   token budget. The 50,000-token budget is frozen and observed, not claimed as
   a provider-side hard ceiling.
3. **Provider secret provisioning.** The public Registry accepts only an
   operator-owned credential reference. It intentionally does not accept raw
   secrets. A real run therefore requires the selected `NICO_TOOL_SECRET_*`
   variable to exist in both the Nico API and Worker environments before the
   benchmark starts.
4. **Reachability profile.** Plain HTTP Providers are allowed only on explicitly
   enabled loopback deployments. A containerized Nico Worker requires an
   operator-configured trusted endpoint (normally TLS); Nico Bench will not
   expose the listener to a broad Docker or LAN network automatically.

These gaps are reported as benchmark degradations. None is replaced with a
database write, internal runtime call, Tool-result fabrication, or fallback
executor.

## Go/no-go

The architecture is **GO** for a colocated, explicitly configured development
deployment where Nico's loopback Provider policy is enabled and the Provider
credential reference is pre-provisioned. A live run is **NO-GO** when any
operator credential, exact AgentVersion, DeepSeek endpoint, loopback Provider
policy, Docker/Harbor prerequisite, or immutable upstream lock is missing.
That state must be reported as a capability/configuration gap rather than
worked around.
