import { useQuery } from "@tanstack/react-query";

import { ComponentHealth, fetchReadiness } from "./api";
import { RunInspector } from "./run-inspector";
import "./styles.css";

const COMPONENTS = [
  {
    key: "postgres",
    name: "PostgreSQL + pgvector",
    short: "PG",
    purpose: "任务状态、记忆与语义索引",
  },
  { key: "redis", name: "Redis", short: "RD", purpose: "运行协调与事件传递" },
  { key: "minio", name: "MinIO", short: "IO", purpose: "执行产物持久化" },
] as const;

const CAPABILITIES = ["可恢复 Run", "受控 Memory", "版本化 Skill"] as const;

const TIME_FORMATTER = new Intl.DateTimeFormat("zh-CN", {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

export function App() {
  const readiness = useQuery({
    queryKey: ["platform-readiness"],
    queryFn: ({ signal }) => fetchReadiness(signal),
    retry: false,
    refetchInterval: 10_000,
  });

  const checkedAt = readiness.data?.checked_at
    ? TIME_FORMATTER.format(new Date(readiness.data.checked_at))
    : null;

  return (
    <main className="shell">
      <header className="topbar">
        <a className="brand" href="/" aria-label="Nico Agent Platform 首页">
          <img className="brand-mark" src="/brand/nico-logo.png" alt="" />
          <span>
            <strong>Nico Agent</strong>
            <small>GROWING AGENT PLATFORM</small>
          </span>
        </a>
        <nav className="topnav" aria-label="平台导航">
          <a href="#run-inspector">Run Inspector</a>
          <a href="/openapi.json">OpenAPI</a>
          <a className="api-link" href="/docs">
            API 文档 <span aria-hidden="true">↗</span>
          </a>
        </nav>
      </header>

      <section className="hero" aria-labelledby="hero-title">
        <div className="hero-copy">
          <p className="eyebrow">GENERAL-PURPOSE AGENT RUNTIME</p>
          <h1 id="hero-title">
            可靠执行
            <span>受控成长</span>
          </h1>
          <p className="lede">
            用统一服务管理 Agent、任务、工具、记忆与 Skill。业务系统决定团队如何协作，
            Nico 负责让每一个 Agent 安全、可恢复、可审计地完成工作。
          </p>
          <ul className="capability-list" aria-label="核心能力">
            {CAPABILITIES.map((capability) => (
              <li key={capability}>{capability}</li>
            ))}
          </ul>
        </div>
        <div className="hero-visual" aria-hidden="true">
          <div className="pixel-frame">
            <span className="frame-corner corner-a" />
            <span className="frame-corner corner-b" />
            <span className="frame-corner corner-c" />
            <span className="frame-corner corner-d" />
            <div className="pixel-halo" />
            <img src="/brand/nico-logo.png" alt="" />
          </div>
          <p>NICO // ONLINE</p>
        </div>
      </section>

      <section className="status-section" aria-labelledby="status-title">
        <div className="section-heading">
          <div>
            <p className="eyebrow">LIVE INFRASTRUCTURE</p>
            <h2 id="status-title">服务状态</h2>
            <p>实时检查 Nico 运行所依赖的核心服务。</p>
          </div>
          <StatusSummary
            pending={readiness.isPending}
            failed={readiness.isError}
            ready={readiness.data?.status === "ready"}
          />
        </div>

        {readiness.isPending ? (
          <section className="notice loading" role="status">
            <span className="pulse" aria-hidden="true" />
            <div>
              <strong>正在连接基础设施</strong>
              <p>等待 API 返回 PostgreSQL、Redis 与 MinIO 的实时探针结果。</p>
            </div>
          </section>
        ) : readiness.isError ? (
          <section className="notice error" role="alert">
            <span className="notice-symbol" aria-hidden="true">
              !
            </span>
            <div>
              <strong>无法读取平台状态</strong>
              <p>{readiness.error.message}</p>
            </div>
            <button
              type="button"
              onClick={() => readiness.refetch()}
              disabled={readiness.isFetching}
            >
              {readiness.isFetching ? "检查中…" : "重新检查"}
            </button>
          </section>
        ) : (
          <>
          <section className="component-grid" aria-label="基础设施组件">
            {COMPONENTS.map((metadata, index) => (
              <ComponentCard
                key={metadata.key}
                metadata={metadata}
                health={readiness.data.components[metadata.key]}
                index={index + 1}
              />
            ))}
          </section>

          <footer className="status-footer">
            <span>最近检查 {checkedAt ?? "—"}</span>
            <button type="button" onClick={() => readiness.refetch()} disabled={readiness.isFetching}>
              <span className={readiness.isFetching ? "refresh spinning" : "refresh"}>↻</span>
              {readiness.isFetching ? "检查中" : "立即刷新"}
            </button>
          </footer>
          </>
        )}
      </section>
      <RunInspector />
    </main>
  );
}

function StatusSummary({
  pending,
  failed,
  ready,
}: {
  pending: boolean;
  failed: boolean;
  ready: boolean;
}) {
  const { state, label } = summaryPresentation(pending, failed, ready);

  return (
    <div className={`summary ${state}`} role="status">
      <span className="summary-light" aria-hidden="true" />
      <div>
        <small>PLATFORM STATUS</small>
        <strong>{label}</strong>
      </div>
    </div>
  );
}

function summaryPresentation(pending: boolean, failed: boolean, ready: boolean) {
  if (pending) return { state: "pending", label: "正在建立连接" };
  if (failed) return { state: "degraded", label: "状态读取失败" };
  if (ready) return { state: "ready", label: "平台基础设施已就绪" };
  return { state: "degraded", label: "基础设施需要关注" };
}

function ComponentCard({
  metadata,
  health,
  index,
}: {
  metadata: (typeof COMPONENTS)[number];
  health: ComponentHealth | undefined;
  index: number;
}) {
  const isUp = health?.status === "up";
  return (
    <article className={`component-card ${isUp ? "up" : "down"}`}>
      <div className="card-index">0{index}</div>
      <div className="component-icon" aria-hidden="true">
        {metadata.short}
      </div>
      <div className="component-heading">
        <div>
          <h2>{metadata.name}</h2>
          <p>{metadata.purpose}</p>
        </div>
        <span className="component-state">
          <i aria-hidden="true" /> {isUp ? "正常" : "异常"}
        </span>
      </div>
      <dl>
        <div>
          <dt>响应耗时</dt>
          <dd>{health ? `${health.latency_ms.toFixed(1)} ms` : "—"}</dd>
        </div>
        <div>
          <dt>探针来源</dt>
          <dd>Live API</dd>
        </div>
      </dl>
      {!isUp && <p className="component-detail">{health?.detail ?? "探针未返回组件状态"}</p>}
    </article>
  );
}
