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

function Badge({ children, tone }: { children: React.ReactNode; tone: "paper" | "safe" }) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
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
      ) : null}
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

  return (
    <main className="dashboard-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">Kalshi-native MLB paper trading</p>
          <h1>HOMERUN</h1>
        </div>
        <div className="badge-row" aria-label="Trading safety mode">
          <Badge tone="paper">Paper Mode</Badge>
          <Badge tone="safe">Live Trading Disabled</Badge>
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
