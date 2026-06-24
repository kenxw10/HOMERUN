"use client";

import { useEffect, useMemo, useState } from "react";

type PortfolioPoint = {
  timestamp: string;
  value: number;
};

type PerformanceMetrics = {
  win_rate: number | null;
  roi: number | null;
  profit_loss: number;
  record: string;
};

type PositionSummary = {
  market: string;
  side: "yes" | "no";
  entry_price: number;
  current_price: number | null;
  quantity: number;
  status: string;
  resolution: string | null;
};

type DashboardSummary = {
  portfolio_series: PortfolioPoint[];
  performance: PerformanceMetrics;
  positions: PositionSummary[];
  bot: {
    mode: "paper";
    paper_trading: boolean;
    live_trading_enabled: boolean;
    execution_kill_switch: boolean;
    kalshi_env: string;
  };
  model_status: {
    active_model_version: string | null;
    last_training_run: string | null;
    last_calibration_run: string | null;
    candidate_count: number;
    notes: string;
  };
};

type SystemStatus = {
  backend: {
    ready: boolean;
    service: string;
    app_env: string;
  };
  database: {
    ready: boolean;
    configured: boolean;
    dialect: string | null;
    message: string;
  };
  config: {
    ready: boolean;
    paper_trading: boolean;
    live_trading_enabled: boolean;
    execution_kill_switch: boolean;
    kalshi_env: string;
    kalshi_credentials: "not_set" | "set_redacted";
  };
};

type DashboardState =
  | { status: "loading" }
  | { status: "ready"; summary: DashboardSummary; system: SystemStatus }
  | { status: "error"; message: string };

const emptySummary: DashboardSummary = {
  portfolio_series: [],
  performance: {
    win_rate: null,
    roi: null,
    profit_loss: 0,
    record: "0-0-0",
  },
  positions: [],
  bot: {
    mode: "paper",
    paper_trading: true,
    live_trading_enabled: false,
    execution_kill_switch: true,
    kalshi_env: "demo",
  },
  model_status: {
    active_model_version: null,
    last_training_run: null,
    last_calibration_run: null,
    candidate_count: 0,
    notes: "Waiting for API data.",
  },
};

function formatPercent(value: number | null): string {
  if (value === null) {
    return "N/A";
  }

  return new Intl.NumberFormat("en-US", {
    style: "percent",
    maximumFractionDigits: 1,
  }).format(value);
}

function formatCurrency(value: number): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(value);
}

function formatShortDate(value: string): string {
  const date = new Date(value);

  if (Number.isNaN(date.getTime())) {
    return value;
  }

  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
  }).format(date);
}

function getApiBaseUrl(): string {
  return (process.env.NEXT_PUBLIC_API_BASE_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
}

async function fetchJson<T>(url: string): Promise<T> {
  const response = await fetch(url, { cache: "no-store" });

  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }

  return response.json() as Promise<T>;
}

type SafetyBadge = {
  label: string;
  tone: "paper" | "safe" | "danger";
};

function Badge({ children, tone }: { children: React.ReactNode; tone: SafetyBadge["tone"] }) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

function getSafetyBadges(state: DashboardState): SafetyBadge[] {
  if (state.status === "loading") {
    return [
      { label: "Loading Mode", tone: "paper" },
      { label: "Safety Unknown", tone: "danger" },
    ];
  }

  if (state.status === "error") {
    return [
      { label: "API Unavailable", tone: "danger" },
      { label: "Safety Unknown", tone: "danger" },
    ];
  }

  const config = state.system.config;

  return [
    {
      label: config.paper_trading ? "Paper Mode" : "Paper Mode Off",
      tone: config.paper_trading ? "paper" : "danger",
    },
    {
      label: config.live_trading_enabled ? "Live Trading Enabled" : "Live Trading Disabled",
      tone: config.live_trading_enabled ? "danger" : "safe",
    },
    {
      label: config.execution_kill_switch ? "Kill Switch On" : "Kill Switch Off",
      tone: config.execution_kill_switch ? "safe" : "danger",
    },
  ];
}

function StatCard({ label, value, detail }: { label: string; value: string; detail: string }) {
  return (
    <section className="stat-card" aria-label={label}>
      <p>{label}</p>
      <strong>{value}</strong>
      <span>{detail}</span>
    </section>
  );
}

function getPortfolioChart(series: PortfolioPoint[]) {
  const width = 640;
  const height = 220;
  const left = 48;
  const right = 32;
  const top = 24;
  const bottom = 44;
  const values = series.map((point) => point.value);
  const minValue = Math.min(...values);
  const maxValue = Math.max(...values);
  const valueRange = maxValue - minValue || 1;
  const xRange = width - left - right;
  const yRange = height - top - bottom;

  const points = series.map((point, index) => {
    const x = series.length === 1 ? left + xRange / 2 : left + (index / (series.length - 1)) * xRange;
    const y = top + ((maxValue - point.value) / valueRange) * yRange;

    return { ...point, x, y };
  });

  const path = points
    .map((point, index) => `${index === 0 ? "M" : "L"} ${point.x.toFixed(2)} ${point.y.toFixed(2)}`)
    .join(" ");

  return {
    width,
    height,
    minValue,
    maxValue,
    path,
    points,
    latestPoint: points[points.length - 1],
  };
}

function PortfolioChart({ series }: { series: PortfolioPoint[] }) {
  const chart = getPortfolioChart(series);

  return (
    <div
      className="chart-live"
      role="img"
      aria-label={`Portfolio value chart with ${series.length} snapshot${series.length === 1 ? "" : "s"}`}
    >
      <svg viewBox={`0 0 ${chart.width} ${chart.height}`} preserveAspectRatio="none" aria-hidden="true">
        <line x1="48" y1="24" x2="48" y2="176" />
        <line x1="48" y1="176" x2="608" y2="176" />
        <line x1="48" y1="128" x2="608" y2="128" className="grid-line" />
        <line x1="48" y1="80" x2="608" y2="80" className="grid-line" />
        <path d={chart.path} />
        {chart.points.map((point) => (
          <circle key={`${point.timestamp}-${point.value}`} cx={point.x} cy={point.y} r="4" />
        ))}
      </svg>
      <div className="chart-summary">
        <span>Latest value</span>
        <strong>{formatCurrency(chart.latestPoint.value)}</strong>
        <small>{formatShortDate(chart.latestPoint.timestamp)}</small>
      </div>
      <div className="chart-scale">
        <span>{formatCurrency(chart.maxValue)}</span>
        <span>{formatCurrency(chart.minValue)}</span>
      </div>
    </div>
  );
}

function PortfolioPanel({ series }: { series: PortfolioPoint[] }) {
  return (
    <section className="panel portfolio-panel">
      <div className="panel-header">
        <div>
          <p className="eyebrow">Portfolio</p>
          <h2>Value</h2>
        </div>
        <span className="panel-note">{series.length} points</span>
      </div>

      {series.length === 0 ? (
        <div className="chart-empty" role="img" aria-label="Empty portfolio value chart">
          <svg viewBox="0 0 640 220" preserveAspectRatio="none" aria-hidden="true">
            <line x1="48" y1="24" x2="48" y2="176" />
            <line x1="48" y1="176" x2="608" y2="176" />
            <line x1="48" y1="128" x2="608" y2="128" className="grid-line" />
            <line x1="48" y1="80" x2="608" y2="80" className="grid-line" />
            <path d="M72 156 C 180 126, 246 126, 344 146 S 512 146, 584 104" />
          </svg>
          <div className="empty-copy">
            <strong>No portfolio snapshots yet</strong>
            <span>Paper trading has not recorded balance history.</span>
          </div>
        </div>
      ) : (
        <PortfolioChart series={series} />
      )}
    </section>
  );
}

function PositionsTable({ positions }: { positions: PositionSummary[] }) {
  return (
    <section className="panel positions-panel">
      <div className="panel-header">
        <div>
          <p className="eyebrow">Contracts</p>
          <h2>Positions</h2>
        </div>
        <span className="panel-note">{positions.length} open</span>
      </div>

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Market</th>
              <th>Side</th>
              <th>Entry price</th>
              <th>Current price</th>
              <th>Quantity</th>
              <th>Status</th>
              <th>Resolution</th>
            </tr>
          </thead>
          <tbody>
            {positions.length === 0 ? (
              <tr>
                <td colSpan={7} className="table-empty">
                  No paper positions yet. Future rows will show Kalshi contract state.
                </td>
              </tr>
            ) : (
              positions.map((position) => (
                <tr key={`${position.market}-${position.side}-${position.entry_price}`}>
                  <td>{position.market}</td>
                  <td>{position.side.toUpperCase()}</td>
                  <td>{position.entry_price.toFixed(2)}</td>
                  <td>{position.current_price === null ? "N/A" : position.current_price.toFixed(2)}</td>
                  <td>{position.quantity}</td>
                  <td>{position.status}</td>
                  <td>{position.resolution ?? "Pending"}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function ModelStatusPanel({ summary }: { summary: DashboardSummary }) {
  return (
    <section className="panel status-panel">
      <div className="panel-header">
        <div>
          <p className="eyebrow">Model</p>
          <h2>Status</h2>
        </div>
      </div>
      <dl className="status-list">
        <div>
          <dt>Active version</dt>
          <dd>{summary.model_status.active_model_version ?? "None"}</dd>
        </div>
        <div>
          <dt>Candidates</dt>
          <dd>{summary.model_status.candidate_count}</dd>
        </div>
        <div>
          <dt>Last training</dt>
          <dd>{summary.model_status.last_training_run ?? "Not run"}</dd>
        </div>
        <div>
          <dt>Last calibration</dt>
          <dd>{summary.model_status.last_calibration_run ?? "Not run"}</dd>
        </div>
      </dl>
      <p className="panel-copy">{summary.model_status.notes}</p>
    </section>
  );
}

function SystemStatusPanel({ system }: { system: SystemStatus | null }) {
  return (
    <section className="panel status-panel">
      <div className="panel-header">
        <div>
          <p className="eyebrow">System</p>
          <h2>Status</h2>
        </div>
      </div>
      {system ? (
        <>
          <dl className="status-list">
            <div>
              <dt>Backend</dt>
              <dd>{system.backend.ready ? "Ready" : "Not ready"}</dd>
            </div>
            <div>
              <dt>Database</dt>
              <dd>{system.database.configured ? "Configured" : "Not configured"}</dd>
            </div>
            <div>
              <dt>Kalshi env</dt>
              <dd>{system.config.kalshi_env}</dd>
            </div>
            <div>
              <dt>Credentials</dt>
              <dd>{system.config.kalshi_credentials === "set_redacted" ? "Set" : "Not set"}</dd>
            </div>
          </dl>
          <p className="panel-copy">{system.database.message}</p>
        </>
      ) : (
        <p className="panel-copy">System status is unavailable until the API responds.</p>
      )}
    </section>
  );
}

export default function DashboardPage() {
  const [state, setState] = useState<DashboardState>({ status: "loading" });
  const apiBaseUrl = useMemo(() => getApiBaseUrl(), []);

  useEffect(() => {
    let cancelled = false;

    async function loadDashboard() {
      try {
        const [summary, system] = await Promise.all([
          fetchJson<DashboardSummary>(`${apiBaseUrl}/v1/dashboard/summary`),
          fetchJson<SystemStatus>(`${apiBaseUrl}/v1/system/status`),
        ]);

        if (!cancelled) {
          setState({ status: "ready", summary, system });
        }
      } catch (error) {
        const detail = error instanceof Error ? error.message : "Unknown error";

        if (!cancelled) {
          setState({
            status: "error",
            message: `Could not reach the HOMERUN API at ${apiBaseUrl}. ${detail}`,
          });
        }
      }
    }

    loadDashboard();

    return () => {
      cancelled = true;
    };
  }, [apiBaseUrl]);

  const summary = state.status === "ready" ? state.summary : emptySummary;
  const system = state.status === "ready" ? state.system : null;
  const isLoading = state.status === "loading";
  const safetyBadges = getSafetyBadges(state);

  return (
    <main className="dashboard-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">Kalshi-native MLB paper trading</p>
          <h1>HOMERUN</h1>
        </div>
        <div className="badge-row" aria-label="Trading safety mode">
          {safetyBadges.map((badge) => (
            <Badge key={badge.label} tone={badge.tone}>
              {badge.label}
            </Badge>
          ))}
        </div>
      </header>

      {state.status === "error" ? (
        <section className="api-error" role="alert">
          <strong>API unavailable</strong>
          <span>{state.message}</span>
        </section>
      ) : null}

      {isLoading ? (
        <section className="api-loading" aria-live="polite">
          Loading dashboard state from {apiBaseUrl}
        </section>
      ) : null}

      <section className="stats-grid" aria-label="Performance metrics">
        <StatCard label="Win rate" value={formatPercent(summary.performance.win_rate)} detail="No settled paper trades" />
        <StatCard label="ROI" value={formatPercent(summary.performance.roi)} detail="Hold-to-settlement basis" />
        <StatCard label="P/L" value={formatCurrency(summary.performance.profit_loss)} detail="Paper trading only" />
        <StatCard label="Record" value={summary.performance.record} detail="Wins-losses-pushes" />
      </section>

      <section className="content-grid">
        <PortfolioPanel series={summary.portfolio_series} />
        <div className="side-stack">
          <ModelStatusPanel summary={summary} />
          <SystemStatusPanel system={system} />
        </div>
      </section>

      <PositionsTable positions={summary.positions} />
    </main>
  );
}
