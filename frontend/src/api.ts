export type ComponentStatus = "up" | "down";
export type ReadinessStatus = "ready" | "not_ready";

export interface ComponentHealth {
  status: ComponentStatus;
  latency_ms: number;
  detail: string | null;
}

export interface ReadinessReport {
  status: ReadinessStatus;
  checked_at: string;
  components: Record<string, ComponentHealth>;
}

export async function fetchReadiness(signal?: AbortSignal): Promise<ReadinessReport> {
  const response = await fetch("/api/v1/health/ready", {
    headers: { Accept: "application/json" },
    signal,
  });

  if (response.status !== 200 && response.status !== 503) {
    throw new Error(`健康检查返回 HTTP ${response.status}`);
  }

  const payload: unknown = await response.json();
  if (!isReadinessReport(payload)) {
    throw new Error("健康检查返回了无法识别的数据");
  }
  return payload;
}

function isReadinessReport(value: unknown): value is ReadinessReport {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<ReadinessReport>;
  if (candidate.status !== "ready" && candidate.status !== "not_ready") return false;
  if (
    typeof candidate.checked_at !== "string" ||
    Number.isNaN(Date.parse(candidate.checked_at))
  )
    return false;
  if (
    !candidate.components ||
    typeof candidate.components !== "object" ||
    Array.isArray(candidate.components)
  )
    return false;

  return Object.values(candidate.components).every(
    (component) =>
      component !== null &&
      typeof component === "object" &&
      (component.status === "up" || component.status === "down") &&
      typeof component.latency_ms === "number" &&
      Number.isFinite(component.latency_ms) &&
      component.latency_ms >= 0 &&
      (component.detail === null || typeof component.detail === "string"),
  );
}
