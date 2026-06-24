"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

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
  time_entered: string | null;
  time_entered_display: string | null;
  market: string;
  side: "yes" | "no";
  entry_price: number;
  current_price: number | null;
  quantity: number;
  profit_loss: number | null;
  profit_loss_percent: number | null;
  status: string;
  resolution: string | null;
};

type DashboardSummary = {
  portfolio_series: PortfolioPoint[];
  performance: PerformanceMetrics;
  positions: PositionSummary[];
  cash_balance: number | null;
  portfolio_value: number | null;
  last_update: string | null;
  last_update_display: string | null;
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
  | { status: "error"; message: string; summary: DashboardSummary | null; system: SystemStatus | null };

type StatusRow = {
  label: string;
  value: string;
  tone?: "green" | "amber" | "red";
};

const chartControls = ["LIVE", "30M", "1D", "1W", "1M", "ALL", "12H", "NORM", "P/L $", "P/L %"];

const emptySummary: DashboardSummary = {
  portfolio_series: [],
  performance: {
    win_rate: null,
    roi: null,
    profit_loss: 0,
    record: "0-0-0",
  },
  positions: [],
  cash_balance: null,
  portfolio_value: null,
  last_update: null,
  last_update_display: null,
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
    notes: "No model has been trained yet.",
  },
};

function getApiBaseUrl(): string {
  return (process.env.NEXT_PUBLIC_API_BASE_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
}

function getRefreshMs(): number {
  const parsed = Number(process.env.NEXT_PUBLIC_REFRESH_MS || "30000");
  return Number.isFinite(parsed) && parsed >= 5000 ? parsed : 30000;
}

async function fetchJson<T>(url: string): Promise<T> {
  const response = await fetch(url, { cache: "no-store" });

  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }

  return response.json() as Promise<T>;
}

function formatCurrency(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "N/A";
  }

  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value);
}

function formatSignedCurrency(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "N/A";
  }
  const formatted = formatCurrency(Math.abs(value));
  return value > 0 ? `+${formatted}` : value < 0 ? `-${formatted}` : formatted;
}

function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "N/A";
  }

  return new Intl.NumberFormat("en-US", {
    style: "percent",
    maximumFractionDigits: 2,
  }).format(value);
}

function formatPrice(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "N/A";
  }

  if (value <= 1) {
    return `${Math.round(value * 100)}C`;
  }

  return formatCurrency(value);
}

function formatEastern(value: string | null | undefined): string {
  if (!value) {
    return "N/A";
  }

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }

  return new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    month: "short",
    day: "2-digit",
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
  }).format(date);
}

function pctClass(value: number | null | undefined): string {
  if (value === null || value === undefined || value === 0) {
    return "value-neutral";
  }
  return value > 0 ? "value-positive" : "value-negative";
}

function StatusPill({ children, tone }: { children: React.ReactNode; tone: "paper" | "safe" | "danger" | "muted" }) {
  return <span className={`status-pill status-${tone}`}>{children}</span>;
}

function Header({
  summary,
  system,
  connected,
  onRefresh,
}: {
  summary: DashboardSummary;
  system: SystemStatus | null;
  connected: boolean;
  onRefresh: () => void;
}) {
  const paperEnabled = system?.config.paper_trading ?? summary.bot.paper_trading;
  const liveEnabled = system?.config.live_trading_enabled ?? summary.bot.live_trading_enabled;
  const killSwitchOn = system?.config.execution_kill_switch ?? summary.bot.execution_kill_switch;

  return (
    <header className="terminal-header">
      <div className="brand-block">
        <h1>HOMERUN</h1>
        <p>KALSHI-NATIVE MLB PAPER TRADING</p>
      </div>

      <div className="header-strip" aria-label="Trading system state">
        <span>
          MODE: <b className="amber">{paperEnabled ? "PAPER" : "UNKNOWN"}</b>
        </span>
        <span>
          LIVE TRADING: <b className={liveEnabled ? "red" : "green"}>{liveEnabled ? "ENABLED" : "DISABLED"}</b>
        </span>
        <span>
          KILL SWITCH: <b className={killSwitchOn ? "green" : "red"}>{killSwitchOn ? "ON" : "OFF"}</b>
        </span>
        <span>CASH: <b className="green">{formatCurrency(summary.cash_balance)}</b></span>
        <span>PORTFOLIO: <b className="amber">{formatCurrency(summary.portfolio_value)}</b></span>
        <button type="button" className="refresh-button" onClick={onRefresh} aria-label="Refresh dashboard">
          REFRESH
        </button>
        <span>LAST UPDATE: {summary.last_update_display ?? formatEastern(summary.last_update)}</span>
        <StatusPill tone={connected ? "safe" : "danger"}>{connected ? "API CONNECTED" : "API DISCONNECTED"}</StatusPill>
      </div>
    </header>
  );
}

function buildChart(series: PortfolioPoint[]) {
  const width = 1200;
  const height = 260;
  const padding = { top: 24, right: 28, bottom: 34, left: 54 };
  const values = series.map((point) => point.value);
  const min = values.length ? Math.min(...values) : 24;
  const max = values.length ? Math.max(...values) : 40;
  const yMin = Math.floor((min - 2) / 2) * 2;
  const yMax = Math.ceil((max + 2) / 2) * 2;
  const yRange = yMax - yMin || 1;
  const xRange = width - padding.left - padding.right;
  const yPixels = height - padding.top - padding.bottom;

  const points = series.map((point, index) => {
    const x = series.length === 1 ? padding.left + xRange : padding.left + (index / (series.length - 1)) * xRange;
    const y = padding.top + ((yMax - point.value) / yRange) * yPixels;
    return { ...point, x, y };
  });

  return {
    width,
    height,
    yMin,
    yMax,
    points,
    polyline: points.map((point) => `${point.x.toFixed(2)},${point.y.toFixed(2)}`).join(" "),
    latest: points[points.length - 1],
  };
}

function PortfolioChart({ summary }: { summary: DashboardSummary }) {
  const chart = buildChart(summary.portfolio_series);
  const latest = summary.portfolio_value ?? chart.latest?.value ?? null;
  const first = summary.portfolio_series[0]?.value ?? null;
  const changePct = first && latest !== null ? (latest - first) / first : null;
  const ticks = [chart.yMax, (chart.yMax + chart.yMin) / 2, chart.yMin];

  return (
    <section className="panel chart-panel">
      <div className="panel-heading chart-heading">
        <div>
          <h2>PORTFOLIO VALUE (PAPER TRADING)</h2>
          <div className="chart-value-row">
            <strong>{formatCurrency(latest)}</strong>
            <span className={pctClass(changePct)}>{formatPercent(changePct)}</span>
          </div>
        </div>
        <div className="chart-controls" aria-label="Static chart controls">
          {chartControls.map((control) => (
            <button key={control} className={control === "12H" ? "active" : ""} type="button">
              {control}
            </button>
          ))}
        </div>
      </div>

      <div className="chart-stage" role="img" aria-label="Portfolio value line chart">
        <svg viewBox={`0 0 ${chart.width} ${chart.height}`} preserveAspectRatio="none" aria-hidden="true">
          {[0, 1, 2, 3, 4].map((index) => {
            const y = 24 + index * 50;
            return <line key={`h-${index}`} x1="54" y1={y} x2="1172" y2={y} className="chart-grid" />;
          })}
          {[0, 1, 2, 3, 4, 5, 6].map((index) => {
            const x = 54 + index * 186;
            return <line key={`v-${index}`} x1={x} y1="24" x2={x} y2="226" className="chart-grid" />;
          })}
          {summary.portfolio_series.length > 0 ? (
            <>
              <polyline points={chart.polyline} />
              {chart.latest ? (
                <g>
                  <line x1={chart.latest.x} y1={chart.latest.y} x2="1172" y2={chart.latest.y} className="last-guide" />
                  <rect x="1130" y={chart.latest.y - 11} width="56" height="22" className="last-tag-bg" />
                  <text x="1138" y={chart.latest.y + 5} className="last-tag-text">
                    {formatCurrency(chart.latest.value)}
                  </text>
                </g>
              ) : null}
            </>
          ) : (
            <g>
              <polyline points="54,158 240,152 426,156 612,150 798,154 984,151 1172,153" className="empty-line" />
              <text x="600" y="126" textAnchor="middle" className="empty-chart-label">
                NO PORTFOLIO SNAPSHOTS
              </text>
            </g>
          )}
        </svg>
        <div className="chart-y-axis">
          {ticks.map((tick) => (
            <span key={tick}>{formatCurrency(tick)}</span>
          ))}
        </div>
        <div className="chart-x-axis">
          <span>08:00</span>
          <span>10:00</span>
          <span>12:00</span>
          <span>14:00</span>
          <span>16:00</span>
          <span>18:00</span>
          <span>20:00</span>
        </div>
      </div>
    </section>
  );
}

function MetricCard({ label, value, detail }: { label: string; value: string; detail: string }) {
  return (
    <section className="panel metric-card">
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </section>
  );
}

function PositionsTable({ positions }: { positions: PositionSummary[] }) {
  return (
    <section className="panel positions-panel">
      <div className="panel-heading positions-heading">
        <h2>OPEN POSITIONS</h2>
        <span>{positions.length} OPEN POSITIONS</span>
      </div>
      <div className="terminal-table-wrap">
        <table className="terminal-table">
          <thead>
            <tr>
              <th>TIME ENTERED (EDT/EST)</th>
              <th>MARKET</th>
              <th>SIDE</th>
              <th>ENTRY PRICE</th>
              <th>CURRENT PRICE</th>
              <th>QTY</th>
              <th>P/L ($)</th>
              <th>P/L (%)</th>
              <th>STATUS</th>
              <th>RESOLUTION</th>
            </tr>
          </thead>
          <tbody>
            {positions.length === 0 ? (
              <tr>
                <td colSpan={10} className="table-empty">
                  <b>NO OPEN POSITIONS</b>
                  <span>PAPER TRADING HAS NOT TAKEN ANY POSITIONS YET.</span>
                </td>
              </tr>
            ) : (
              positions.map((position) => (
                <tr key={`${position.market}-${position.side}-${position.time_entered}-${position.entry_price}`}>
                  <td>{position.time_entered_display ?? formatEastern(position.time_entered)}</td>
                  <td>{position.market}</td>
                  <td>{position.side}</td>
                  <td>{formatPrice(position.entry_price)}</td>
                  <td>{formatPrice(position.current_price)}</td>
                  <td>{position.quantity}</td>
                  <td className={pctClass(position.profit_loss)}>{formatSignedCurrency(position.profit_loss)}</td>
                  <td className={pctClass(position.profit_loss_percent)}>{formatPercent(position.profit_loss_percent)}</td>
                  <td>{position.status}</td>
                  <td>{position.resolution ?? "PENDING"}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function StatusTable({
  title,
  rows,
  note,
}: {
  title: string;
  rows: StatusRow[];
  note?: string;
}) {
  return (
    <section className="panel status-panel">
      <div className="panel-heading">
        <h2>{title}</h2>
      </div>
      <div className="status-rows">
        {rows.map((row) => (
          <div key={row.label}>
            <span>{row.label}</span>
            <b className={row.tone ?? ""}>{row.value}</b>
          </div>
        ))}
      </div>
      {note ? <p>{note}</p> : null}
    </section>
  );
}

export default function DashboardPage() {
  const apiBaseUrl = useMemo(() => getApiBaseUrl(), []);
  const refreshMs = useMemo(() => getRefreshMs(), []);
  const [state, setState] = useState<DashboardState>({ status: "loading" });

  const loadDashboard = useCallback(async () => {
    try {
      const [summary, system] = await Promise.all([
        fetchJson<DashboardSummary>(`${apiBaseUrl}/v1/dashboard/summary`),
        fetchJson<SystemStatus>(`${apiBaseUrl}/v1/system/status`),
      ]);
      setState({ status: "ready", summary, system });
    } catch (error) {
      const detail = error instanceof Error ? error.message : "UNKNOWN ERROR";
      setState((previous) => ({
        status: "error",
        message: `API UNAVAILABLE AT ${apiBaseUrl}: ${detail}`,
        summary: previous.status === "ready" ? previous.summary : null,
        system: previous.status === "ready" ? previous.system : null,
      }));
    }
  }, [apiBaseUrl]);

  useEffect(() => {
    loadDashboard();
    const timer = window.setInterval(loadDashboard, refreshMs);
    return () => window.clearInterval(timer);
  }, [loadDashboard, refreshMs]);

  const summary = state.status === "ready" ? state.summary : state.status === "error" && state.summary ? state.summary : emptySummary;
  const system = state.status === "ready" ? state.system : state.status === "error" ? state.system : null;
  const connected = state.status === "ready";

  const modelRows: StatusRow[] = [
    { label: "ACTIVE MODEL VERSION", value: summary.model_status.active_model_version ?? "NONE" },
    { label: "CANDIDATES", value: String(summary.model_status.candidate_count) },
    { label: "LAST TRAINING", value: formatEastern(summary.model_status.last_training_run) },
    { label: "LAST CALIBRATION", value: formatEastern(summary.model_status.last_calibration_run) },
  ];

  const systemRows: StatusRow[] = [
    { label: "BACKEND", value: system?.backend.ready ? "READY" : "NOT READY", tone: system?.backend.ready ? "green" : "red" },
    { label: "DATABASE", value: system?.database.ready ? "READY" : "NOT READY", tone: system?.database.ready ? "green" : "red" },
    { label: "KALSHI ENV", value: system?.config.kalshi_env ?? summary.bot.kalshi_env, tone: "amber" },
    {
      label: "CREDENTIALS",
      value: system?.config.kalshi_credentials === "set_redacted" ? "SET" : "NOT SET",
      tone: system?.config.kalshi_credentials === "set_redacted" ? "green" : "amber",
    },
    {
      label: "PAPER / LIVE / KILL",
      value: `${system?.config.paper_trading ? "PAPER" : "NO PAPER"} / ${
        system?.config.live_trading_enabled ? "LIVE ON" : "LIVE OFF"
      } / ${system?.config.execution_kill_switch ? "KILL ON" : "KILL OFF"}`,
      tone: system?.config.ready ? "green" : "red",
    },
  ];

  return (
    <main className="dashboard-shell">
      <Header summary={summary} system={system} connected={connected} onRefresh={loadDashboard} />

      {state.status === "error" ? (
        <section className="api-banner" role="alert">
          {state.message}
        </section>
      ) : null}

      <PortfolioChart summary={summary} />

      <section className="metrics-grid" aria-label="Performance metrics">
        <MetricCard label="WIN RATE" value={formatPercent(summary.performance.win_rate)} detail="NO SETTLED PAPER TRADES" />
        <MetricCard label="ROI" value={formatPercent(summary.performance.roi)} detail="HOLD-TO-SETTLEMENT BASIS" />
        <MetricCard label="P/L" value={formatSignedCurrency(summary.performance.profit_loss)} detail="PAPER TRADING ONLY" />
        <MetricCard label="RECORD" value={summary.performance.record} detail="WINS-LOSSES-PUSHES" />
      </section>

      <PositionsTable positions={summary.positions} />

      <section className="status-grid">
        <StatusTable title="MODEL STATUS" rows={modelRows} note={summary.model_status.notes} />
        <StatusTable title="SYSTEM STATUS" rows={systemRows} note={system?.database.message ?? "DATABASE STATUS UNAVAILABLE."} />
      </section>

      <footer className="terminal-footer">
        <span>ALL TIMES DISPLAYED IN EDT/EST</span>
        <span>HOMERUN V0.2.0 | PR2 DATA LAYER</span>
      </footer>
    </main>
  );
}
