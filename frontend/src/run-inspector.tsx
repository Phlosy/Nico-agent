import { useQuery } from "@tanstack/react-query";
import { FormEvent, ReactNode, useMemo, useState } from "react";

type JsonObject = Record<string, unknown>;

interface SectionResult<T> {
  data: T;
  error: string | null;
}

interface RunInspectionData {
  run: JsonObject;
  runtime: SectionResult<JsonObject | null>;
  steps: SectionResult<JsonObject[]>;
  modelCalls: SectionResult<JsonObject[]>;
  plans: SectionResult<JsonObject[]>;
  planSteps: SectionResult<JsonObject[]>;
  children: SectionResult<JsonObject[]>;
  delegations: SectionResult<JsonObject[]>;
  messages: SectionResult<JsonObject[]>;
  artifacts: SectionResult<JsonObject[]>;
  events: SectionResult<JsonObject[]>;
}

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const REDACTED_KEY = /(secret|password|authorization|credential|api[_-]?key|lease_token|mcp_token|chain_of_thought|hidden_reasoning)/i;

export function RunInspector() {
  const initial = useMemo(() => new URLSearchParams(window.location.search), []);
  const [tenantId, setTenantId] = useState(initial.get("tenant") ?? "");
  const [runId, setRunId] = useState(initial.get("run") ?? "");
  const [selection, setSelection] = useState(() => ({
    tenantId: initial.get("tenant") ?? "",
    runId: initial.get("run") ?? "",
  }));
  const valid = UUID_PATTERN.test(selection.tenantId) && UUID_PATTERN.test(selection.runId);
  const inspection = useQuery({
    queryKey: ["run-inspection", selection.tenantId, selection.runId],
    queryFn: ({ signal }) =>
      fetchRunInspection(selection.tenantId, selection.runId, signal),
    enabled: valid,
    retry: false,
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const next = { tenantId: tenantId.trim(), runId: runId.trim() };
    setSelection(next);
    if (UUID_PATTERN.test(next.tenantId) && UUID_PATTERN.test(next.runId)) {
      const params = new URLSearchParams(window.location.search);
      params.set("tenant", next.tenantId);
      params.set("run", next.runId);
      window.history.replaceState(null, "", `?${params.toString()}#run-inspector`);
    }
  }

  const invalidSelection =
    Boolean(selection.tenantId || selection.runId) && !valid;

  return (
    <section className="inspector-section" id="run-inspector" aria-labelledby="inspector-title">
      <div className="section-heading inspector-heading">
        <div>
          <p className="eyebrow">READ-ONLY RUN INSPECTOR</p>
          <h2 id="inspector-title">运行检查器</h2>
          <p>按租户边界读取一次 Run 的结果、轨迹、协作与审计事实。</p>
        </div>
        <span className="read-only-badge">只读 · 已脱敏</span>
      </div>

      <form className="inspector-form" onSubmit={submit} aria-label="选择要检查的运行">
        <label>
          <span>Tenant ID</span>
          <input
            name="tenant"
            value={tenantId}
            onChange={(event) => setTenantId(event.target.value)}
            placeholder="00000000-0000-0000-0000-000000000000"
            autoComplete="off"
          />
        </label>
        <label>
          <span>Run ID</span>
          <input
            name="run"
            value={runId}
            onChange={(event) => setRunId(event.target.value)}
            placeholder="00000000-0000-0000-0000-000000000000"
            autoComplete="off"
          />
        </label>
        <button type="submit">读取运行</button>
      </form>

      {invalidSelection ? (
        <InspectorNotice kind="error">Tenant ID 和 Run ID 必须是完整 UUID。</InspectorNotice>
      ) : !valid ? (
        <InspectorNotice>输入 Tenant ID 与 Run ID，或使用带参数的检查器链接。</InspectorNotice>
      ) : inspection.isPending ? (
        <InspectorNotice kind="loading">正在读取运行事实…</InspectorNotice>
      ) : inspection.isError ? (
        <InspectorNotice kind="error">
          {inspection.error instanceof Error ? inspection.error.message : "运行读取失败"}
        </InspectorNotice>
      ) : (
        <InspectionResult data={inspection.data} />
      )}
    </section>
  );
}

function InspectionResult({ data }: { data: RunInspectionData }) {
  const status = textValue(data.run.status, "unknown");
  const runtime = data.runtime.data;
  const usagePartial = data.modelCalls.data.some(
    (call) => call.usage_status !== "exact" || call.cost_status !== "exact",
  );

  return (
    <div className="inspection-result" aria-live="polite">
      <InspectorPanel title="状态与结果" index="01">
        <dl className="fact-grid">
          <Fact label="Run 状态" value={status} tone={status} />
          <Fact label="Runtime" value={textValue(runtime?.provider_name, "尚未创建")} />
          <Fact label="执行模式" value={textValue(runtime?.execution_mode, "—")} />
          <Fact label="协议版本" value={textValue(runtime?.protocol_version, "—")} />
        </dl>
        {status === "cancelled" && <p className="state-callout cancelled">该运行已取消。</p>}
        <SafeJson value={data.run.result ?? data.run.error ?? { message: "暂无结果" }} />
        <PartialError result={data.runtime} />
      </InspectorPanel>

      <InspectorPanel title="步骤与模型用量" index="02">
        {usagePartial && data.modelCalls.data.length > 0 && (
          <p className="state-callout partial">部分 Provider 用量或费用为 partial/estimated。</p>
        )}
        <CompactList
          items={data.steps.data}
          empty="尚无执行步骤"
          render={(step) =>
            `${textValue(step.sequence, "—")} · ${textValue(step.step_type ?? step.kind, "step")} · ${textValue(step.status, "unknown")}`
          }
        />
        <CompactList
          items={data.modelCalls.data}
          empty="尚无模型调用"
          render={(call) =>
            `${textValue(call.model, "unknown model")} · ${textValue(call.status, "unknown")} · usage ${textValue(call.usage_status, "partial")}`
          }
        />
        <PartialError result={data.steps} />
        <PartialError result={data.modelCalls} />
      </InspectorPanel>

      <InspectorPanel title="Plan" index="03">
        <CompactList
          items={data.plans.data}
          empty="此运行没有 Plan"
          render={(plan) =>
            `revision ${textValue(plan.revision, "—")} · ${textValue(plan.status, "unknown")} · ${textValue(plan.objective, "")}`
          }
        />
        <CompactList
          items={data.planSteps.data}
          empty="没有 PlanStep"
          render={(step) =>
            `${textValue(step.step_key, "step")} · ${textValue(step.status, "unknown")} · ${textValue(step.title, "")}`
          }
        />
        <PartialError result={data.plans} />
        <PartialError result={data.planSteps} />
      </InspectorPanel>

      <InspectorPanel title="Child tree" index="04">
        <CompactList
          items={data.children.data}
          empty="没有 Child Run"
          render={(child) =>
            `${textValue(child.id, "unknown")} · ${textValue(child.status, "unknown")}`
          }
        />
        <CompactList
          items={data.delegations.data}
          empty="没有 Delegation"
          render={(delegation) =>
            `${textValue(delegation.child_run_id, "unknown")} · ${textValue(delegation.status, "unknown")}`
          }
        />
        <PartialError result={data.children} />
        <PartialError result={data.delegations} />
      </InspectorPanel>

      <InspectorPanel title="消息与 Artifact" index="05">
        <CompactList
          items={data.messages.data}
          empty="没有 Agent Message"
          render={(message) =>
            `${textValue(message.message_type, "message")} · ${textValue(message.status, "unknown")} · ${textValue(message.visibility, "restricted")}`
          }
        />
        <CompactList
          items={data.artifacts.data}
          empty="没有 Artifact"
          render={(artifact) =>
            `${textValue(artifact.name, "artifact")} · ${textValue(artifact.status, "unknown")} · ${textValue(artifact.size_bytes, "0")} bytes`
          }
        />
        <PartialError result={data.messages} />
        <PartialError result={data.artifacts} />
      </InspectorPanel>

      <InspectorPanel title="预算与审计" index="06">
        <SafeJson value={{ budgets: data.run.budgets ?? {}, cost: data.run.cost ?? {} }} />
        <CompactList
          items={data.events.data}
          empty="没有运行审计事件"
          render={(event) =>
            `${textValue(event.sequence, "—")} · ${textValue(event.event_type, "event")}`
          }
        />
        <PartialError result={data.events} />
      </InspectorPanel>
    </div>
  );
}

function InspectorPanel({
  title,
  index,
  children,
}: {
  title: string;
  index: string;
  children: ReactNode;
}) {
  return (
    <article className="inspector-panel" aria-labelledby={`inspector-panel-${index}`}>
      <header>
        <span>{index}</span>
        <h3 id={`inspector-panel-${index}`}>{title}</h3>
      </header>
      {children}
    </article>
  );
}

function Fact({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd className={tone ? `tone-${tone}` : undefined}>{value}</dd>
    </div>
  );
}

function CompactList({
  items,
  empty,
  render,
}: {
  items: JsonObject[];
  empty: string;
  render: (item: JsonObject) => string;
}) {
  if (items.length === 0) return <p className="empty-state">{empty}</p>;
  return (
    <ul className="compact-list">
      {items.map((item, index) => (
        <li key={textValue(item.id, `${index}`)}>{render(item)}</li>
      ))}
    </ul>
  );
}

function SafeJson({ value }: { value: unknown }) {
  return <pre className="safe-json">{JSON.stringify(redact(value), null, 2)}</pre>;
}

function PartialError<T>({ result }: { result: SectionResult<T> }) {
  return result.error ? <p className="partial-error">部分数据不可用：{result.error}</p> : null;
}

function InspectorNotice({
  children,
  kind = "empty",
}: {
  children: ReactNode;
  kind?: "empty" | "loading" | "error";
}) {
  return (
    <div className={`inspector-notice ${kind}`} role={kind === "error" ? "alert" : "status"}>
      {children}
    </div>
  );
}

async function fetchRunInspection(
  tenantId: string,
  runId: string,
  signal: AbortSignal,
): Promise<RunInspectionData> {
  const headers = {
    Accept: "application/json",
    "X-Tenant-ID": tenantId,
    "X-Actor-ID": "nico-console-readonly",
  };
  const path = (suffix: string) => `/api/v1/runs/${encodeURIComponent(runId)}${suffix}`;
  const run = await fetchObject(path(""), headers, signal);
  const [runtime, steps, modelCalls, plans, children, delegations, messages, artifacts, events] =
    await Promise.all([
      optionalObject(path("/runtime"), headers, signal),
      optionalList(path("/steps"), headers, signal),
      optionalList(path("/model-calls"), headers, signal),
      optionalList(path("/plans"), headers, signal),
      optionalList(path("/children"), headers, signal),
      optionalList(path("/delegations"), headers, signal),
      optionalList(path("/messages"), headers, signal),
      optionalList(path("/artifacts"), headers, signal),
      optionalList(path("/events"), headers, signal),
    ]);
  const planStepResults = await Promise.all(
    plans.data.map((plan) =>
      optionalList(path(`/plans/${encodeURIComponent(textValue(plan.id, ""))}/steps`), headers, signal),
    ),
  );
  const planSteps: SectionResult<JsonObject[]> = {
    data: planStepResults.flatMap((result) => result.data),
    error: planStepResults.find((result) => result.error)?.error ?? null,
  };
  return { run, runtime, steps, modelCalls, plans, planSteps, children, delegations, messages, artifacts, events };
}

async function optionalList(
  url: string,
  headers: Record<string, string>,
  signal: AbortSignal,
): Promise<SectionResult<JsonObject[]>> {
  try {
    return { data: await fetchList(url, headers, signal), error: null };
  } catch (error) {
    return { data: [], error: errorMessage(error) };
  }
}

async function optionalObject(
  url: string,
  headers: Record<string, string>,
  signal: AbortSignal,
): Promise<SectionResult<JsonObject | null>> {
  try {
    return { data: await fetchObject(url, headers, signal), error: null };
  } catch (error) {
    return { data: null, error: errorMessage(error) };
  }
}

async function fetchObject(url: string, headers: Record<string, string>, signal: AbortSignal) {
  const value = await fetchJson(url, headers, signal);
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("API 返回了无法识别的对象");
  }
  return value as JsonObject;
}

async function fetchList(url: string, headers: Record<string, string>, signal: AbortSignal) {
  const value = await fetchJson(url, headers, signal);
  if (!Array.isArray(value) || value.some((item) => !item || typeof item !== "object" || Array.isArray(item))) {
    throw new Error("API 返回了无法识别的列表");
  }
  return value as JsonObject[];
}

async function fetchJson(url: string, headers: Record<string, string>, signal: AbortSignal) {
  const response = await fetch(url, { headers, signal });
  if (!response.ok) throw new Error(`API 返回 HTTP ${response.status}`);
  return (await response.json()) as unknown;
}

function redact(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(redact);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(
    Object.entries(value as JsonObject).map(([key, item]) => [
      key,
      REDACTED_KEY.test(key) ? "[REDACTED]" : redact(item),
    ]),
  );
}

function textValue(value: unknown, fallback: string): string {
  if (typeof value === "string" || typeof value === "number") return String(value);
  return fallback;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "未知错误";
}
