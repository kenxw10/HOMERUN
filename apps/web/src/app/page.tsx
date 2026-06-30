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

type ActiveEpochSummary = {
  epoch_key: string;
  display_name: string;
  status: string;
  mode: string;
  starting_balance: number;
  started_at: string | null;
};

type JobRunSummary = {
  job_name: string;
  status: string;
  started_at: string | null;
  completed_at: string | null;
  duration_seconds: number | null;
  target_date: string | null;
  result: Record<string, unknown>;
};

type WebSocketStatusSummary = {
  enabled: boolean;
  running: boolean;
  source: string;
  subscribed_market_count: number;
  last_seen_at: string | null;
  last_message_at: string | null;
  reconnect_count: number;
  stale_count: number;
  last_error: string | null;
};

type PositionSummary = {
  time_entered: string | null;
  time_entered_display: string | null;
  time_closed: string | null;
  time_closed_display: string | null;
  market: string;
  market_ticker: string | null;
  market_display: string | null;
  selection_display: string | null;
  matchup_display: string | null;
  contract_display: string | null;
  normalized_equivalent_display: string | null;
  display_title: string | null;
  display_subtitle: string | null;
  raw_ticker_display: string | null;
  selected_position_rationale: Record<string, unknown>;
  side: "yes" | "no";
  entry_price: number;
  exit_price: number | null;
  current_price: number | null;
  current_price_updated_at: string | null;
  current_price_updated_at_display: string | null;
  quantity: number;
  profit_loss: number | null;
  profit_loss_percent: number | null;
  status: string;
  game_status: string | null;
  game_status_display: string | null;
  resolution: string | null;
  outcome: string | null;
};

type DashboardSummary = {
  active_epoch: ActiveEpochSummary | null;
  portfolio_series: PortfolioPoint[];
  performance: PerformanceMetrics;
  positions: PositionSummary[];
  closed_positions: PositionSummary[];
  closed_positions_date: string | null;
  closed_positions_count: number;
  cash_balance: number | null;
  portfolio_value: number | null;
  paper_starting_balance: number | null;
  performance_by_scope: Record<string, Record<string, unknown>>;
  performance_by_family: Record<string, Record<string, unknown>>;
  decision_breakdown_by_scope: Record<string, Record<string, number>>;
  decision_breakdown_by_family: Record<string, Record<string, number>>;
  latest_candidate_diagnostics: Record<string, unknown>;
  job_status: Record<string, JobRunSummary>;
  websocket_status: WebSocketStatusSummary | null;
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
    active_parameter_version: string | null;
    active_calibration_version: string | null;
    feature_version: string | null;
    calibration_status: string | null;
    last_training_run: string | null;
    last_calibration_run: string | null;
    candidate_count: number;
    resolved_mature_samples: number;
    training_eligible_count: number;
    last_governance_status: string | null;
    trade_policy: Record<string, unknown>;
    trade_caps_used: Record<string, unknown>;
    trade_threshold_policy: Record<string, unknown>;
    data_quality_summary: {
      avg?: number | null;
      feature_version?: string | null;
    };
    feature_completeness: Record<string, unknown>;
    source_statuses: Record<string, unknown>;
    critical_module_warnings: string[];
    lineup_status: string | null;
    starter_status: string | null;
    weather_status: string | null;
    governance_status: string | null;
    notes: string | string[];
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
    kalshi_market_data_source: string;
    kalshi_market_data_base_kind: string;
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

type ChartRange = "TODAY" | "1D" | "1W" | "1M" | "ALL";
type ChartMode = "VALUE" | "P/L $" | "P/L %";
type ChartTickFormat =
  | "TIME"
  | "DAY_DATE"
  | "MONTH_DAY"
  | "DAY_TIME"
  | "FULL_TIME"
  | "YEAR_TIME"
  | "SECOND_TIME"
  | "YEAR_SECOND_TIME";

type ChartDataPoint = {
  timestamp: string;
  value: number;
  time: number;
  breakBefore?: boolean;
};

type ChartDomain = {
  start: number;
  end: number;
};

const EASTERN_TIME_ZONE = "America/New_York";
const chartRanges: ChartRange[] = ["TODAY", "1D", "1W", "1M", "ALL"];
const chartModes: ChartMode[] = ["VALUE", "P/L $", "P/L %"];
const chartRangeLabels: Record<ChartRange, string> = {
  TODAY: "Today",
  "1D": "1D",
  "1W": "1W",
  "1M": "1M",
  ALL: "All",
};
const chartModeLabels: Record<ChartMode, string> = {
  VALUE: "Value",
  "P/L $": "P/L $",
  "P/L %": "P/L %",
};
const trailingRangeMs: Record<Exclude<ChartRange, "TODAY" | "ALL">, number> = {
  "1D": 24 * 60 * 60 * 1000,
  "1W": 7 * 24 * 60 * 60 * 1000,
  "1M": 30 * 24 * 60 * 60 * 1000,
};
const chartHourMs = 60 * 60 * 1000;
const chartDayMs = 24 * chartHourMs;

const emptySummary: DashboardSummary = {
  active_epoch: null,
  portfolio_series: [],
  performance: {
    win_rate: null,
    roi: null,
    profit_loss: 0,
    record: "0-0-0",
  },
  positions: [],
  closed_positions: [],
  closed_positions_date: null,
  closed_positions_count: 0,
  cash_balance: null,
  portfolio_value: null,
  paper_starting_balance: 1000,
  performance_by_scope: {},
  performance_by_family: {},
  decision_breakdown_by_scope: {},
  decision_breakdown_by_family: {},
  latest_candidate_diagnostics: {},
  job_status: {},
  websocket_status: {
    enabled: false,
    running: false,
    source: "rest_fallback",
    subscribed_market_count: 0,
    last_seen_at: null,
    last_message_at: null,
    reconnect_count: 0,
    stale_count: 0,
    last_error: null,
  },
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
    feature_version: null,
    calibration_status: "not_run",
    last_training_run: null,
    last_calibration_run: null,
    candidate_count: 0,
    resolved_mature_samples: 0,
    training_eligible_count: 0,
    last_governance_status: "not_run",
    trade_policy: {},
    trade_caps_used: {},
    trade_threshold_policy: {},
    data_quality_summary: {},
    feature_completeness: {},
    source_statuses: {},
    critical_module_warnings: [],
    active_parameter_version: null,
    active_calibration_version: null,
    lineup_status: "missing",
    starter_status: "missing",
    weather_status: "missing",
    governance_status: "not_run",
    notes: ["No mature model has been trained yet."],
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

function formatSignedPercent(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "N/A";
  }
  const formatted = formatPercent(Math.abs(value));
  return value > 0 ? `+${formatted}` : value < 0 ? `-${formatted}` : formatted;
}

function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "N/A";
  }
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 }).format(value);
}

function formatUnknown(value: unknown): string {
  if (typeof value === "number") {
    return formatNumber(value);
  }
  if (typeof value === "boolean") {
    return value ? "TRUE" : "FALSE";
  }
  if (typeof value === "string") {
    return value.toUpperCase();
  }
  return "N/A";
}

function positionRationaleLabel(position: PositionSummary): string | null {
  const rationale = position.selected_position_rationale ?? {};
  const edge = typeof rationale.probability_edge === "number" ? rationale.probability_edge : null;
  const netEv = typeof rationale.net_expected_value === "number" ? rationale.net_expected_value : null;
  const quality = typeof rationale.data_quality === "number" ? rationale.data_quality : null;
  const pieces = [
    edge !== null ? `EDGE ${formatSignedPercent(edge)}` : null,
    netEv !== null ? `NET EV ${formatSignedCurrency(netEv)}` : null,
    quality !== null ? `QUALITY ${formatNumber(quality)}` : null,
  ].filter(Boolean);
  return pieces.length ? pieces.join(" · ") : null;
}

function featureCompletenessLabel(value: Record<string, unknown>): string {
  const keys = Object.keys(value);
  if (!keys.length) {
    return "N/A";
  }
  const available = keys.filter((key) => {
    const counts = value[key];
    if (typeof counts !== "object" || counts === null || !("available" in counts)) {
      return false;
    }
    const availableCount = (counts as Record<string, unknown>).available;
    return typeof availableCount === "number" && availableCount > 0;
  }).length;
  return `${available}/${keys.length}`;
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

function easternDateString(date: Date = new Date()): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/New_York",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(date);
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}

function shiftDate(value: string, days: number): string {
  const date = new Date(`${value}T12:00:00Z`);
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

function formatDateButton(value: string | null | undefined): string {
  if (!value) {
    return "SELECT DATE";
  }
  const date = new Date(`${value}T12:00:00Z`);
  if (Number.isNaN(date.getTime())) {
    return value.toUpperCase();
  }
  return new Intl.DateTimeFormat("en-US", {
    timeZone: "UTC",
    month: "short",
    day: "2-digit",
    year: "numeric",
  })
    .format(date)
    .toUpperCase();
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
        <span>
          OBSERVATION EPOCH: <b>{summary.active_epoch?.display_name ?? "ACTIVE PAPER"}</b>
        </span>
        <span>STARTING BALANCE: <b>{formatCurrency(summary.active_epoch?.starting_balance ?? summary.paper_starting_balance)}</b></span>
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

function parseChartTime(value: string | null | undefined): number | null {
  if (!value) {
    return null;
  }
  const parsed = new Date(value).getTime();
  return Number.isFinite(parsed) ? parsed : null;
}

function getTimeZoneOffsetMs(date: Date, timeZone: string): number {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).formatToParts(date);
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  const zonedAsUtc = Date.UTC(
    Number(values.year),
    Number(values.month) - 1,
    Number(values.day),
    Number(values.hour),
    Number(values.minute),
    Number(values.second),
  );
  return zonedAsUtc - date.getTime();
}

function easternMidnightMs(now: Date): number {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: EASTERN_TIME_ZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(now);
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  const utcGuess = Date.UTC(Number(values.year), Number(values.month) - 1, Number(values.day), 0, 0, 0);
  return utcGuess - getTimeZoneOffsetMs(new Date(utcGuess), EASTERN_TIME_ZONE);
}

function normalizePortfolioSeries(series: PortfolioPoint[]): ChartDataPoint[] {
  return series
    .map((point) => {
      const time = parseChartTime(point.timestamp);
      return time === null || !Number.isFinite(point.value) ? null : { ...point, time };
    })
    .filter((point): point is ChartDataPoint => point !== null)
    .sort((a, b) => a.time - b.time);
}

function latestKnownValue(points: ChartDataPoint[], time: number, fallback: number): number {
  let value = fallback;
  for (const point of points) {
    if (point.time > time) {
      break;
    }
    value = point.value;
  }
  return value;
}

function chartDomainForRange(
  range: ChartRange,
  nowMs: number,
  epochStartMs: number | null,
  points: ChartDataPoint[],
): ChartDomain {
  const latestPointMs = points.length ? points[points.length - 1].time : null;
  const firstPointMs = points.length ? points[0].time : null;
  const end = Math.max(nowMs, latestPointMs ?? nowMs);
  let start: number;

  if (range === "TODAY") {
    start = easternMidnightMs(new Date(nowMs));
  } else if (range === "ALL") {
    start = epochStartMs ?? firstPointMs ?? end - trailingRangeMs["1D"];
  } else {
    start = end - trailingRangeMs[range];
  }

  if (!Number.isFinite(start) || start >= end) {
    start = end - 60 * 60 * 1000;
  }

  return { start, end };
}

function portfolioChartSeries(
  summary: DashboardSummary,
  range: ChartRange,
  startingBalance: number,
  nowMs: number,
): { domain: ChartDomain; series: ChartDataPoint[]; limitedData: boolean; historyTruncated: boolean } {
  const rawPoints = normalizePortfolioSeries(summary.portfolio_series);
  const epochStartMs = parseChartTime(summary.active_epoch?.started_at);
  const domain = chartDomainForRange(range, nowMs, epochStartMs, rawPoints);
  const hasRealPortfolioValue = typeof summary.portfolio_value === "number";

  if (rawPoints.length === 0 && !hasRealPortfolioValue) {
    return {
      domain,
      series: [],
      limitedData: false,
      historyTruncated: false,
    };
  }

  const anchorPoints = [...rawPoints];
  const firstRawPoint = rawPoints[0];
  const epochAnchorInDomain = epochStartMs !== null && epochStartMs >= domain.start && epochStartMs <= domain.end;

  if (epochStartMs !== null && epochStartMs <= domain.end && (rawPoints.length === 0 || epochStartMs >= domain.start || range === "ALL")) {
    anchorPoints.push({
      timestamp: new Date(epochStartMs).toISOString(),
      time: epochStartMs,
      value: startingBalance,
    });
  }

  anchorPoints.sort((a, b) => a.time - b.time);

  const hasPointAtOrBeforeStart = anchorPoints.some((point) => point.time <= domain.start);
  const startsBeforeFirstReturnedSnapshot = rawPoints.length > 0 && !hasPointAtOrBeforeStart && !epochAnchorInDomain;
  const lineStart =
    startsBeforeFirstReturnedSnapshot
      ? rawPoints[0].time
      : epochAnchorInDomain && epochStartMs > domain.start
        ? epochStartMs
        : domain.start;
  const likelyReturnedWindowIsTruncated = rawPoints.length >= 500;
  const gapBeforeFirstReturnedSnapshot =
    likelyReturnedWindowIsTruncated && firstRawPoint !== undefined && firstRawPoint.time > lineStart;
  const historyTruncated =
    startsBeforeFirstReturnedSnapshot ||
    gapBeforeFirstReturnedSnapshot ||
    (likelyReturnedWindowIsTruncated && range === "ALL" && epochStartMs !== null && rawPoints[0]?.time > epochStartMs);
  const firstValue = startsBeforeFirstReturnedSnapshot ? rawPoints[0].value : latestKnownValue(anchorPoints, lineStart, startingBalance);
  const currentValue =
    typeof summary.portfolio_value === "number"
      ? summary.portfolio_value
      : latestKnownValue(anchorPoints, domain.end, firstValue);
  const points: ChartDataPoint[] = [
    {
      timestamp: new Date(lineStart).toISOString(),
      time: lineStart,
      value: firstValue,
    },
  ];

  for (const point of anchorPoints) {
    if (point.time > lineStart && point.time < domain.end) {
      points.push({
        ...point,
        breakBefore: gapBeforeFirstReturnedSnapshot && point.time === firstRawPoint?.time,
      });
    }
  }

  points.push({
    timestamp: new Date(domain.end).toISOString(),
    time: domain.end,
    value: currentValue,
  });

  const uniquePoints = points.filter((point, index) => index === 0 || point.time !== points[index - 1].time);
  if (uniquePoints.length === 1) {
    uniquePoints.push({
      timestamp: new Date(domain.end).toISOString(),
      time: domain.end,
      value: uniquePoints[0].value,
    });
  }

  return {
    domain,
    series: uniquePoints,
    limitedData: rawPoints.length === 0 || rawPoints.some((point) => point.time < domain.start),
    historyTruncated,
  };
}

function transformPortfolioSeries(series: ChartDataPoint[], mode: ChartMode, startingBalance: number): ChartDataPoint[] {
  return series.map((point) => {
    if (mode === "P/L $") {
      return { ...point, value: point.value - startingBalance };
    }
    if (mode === "P/L %") {
      return { ...point, value: startingBalance > 0 ? (point.value - startingBalance) / startingBalance : 0 };
    }
    return point;
  });
}

function formatChartValue(value: number | null | undefined, mode: ChartMode): string {
  if (mode === "P/L $") {
    return formatSignedCurrency(value);
  }
  if (mode === "P/L %") {
    return formatPercent(value);
  }
  return formatCurrency(value);
}

function formatChartTick(time: number, format: ChartTickFormat): string {
  const date = new Date(time);
  if (format === "TIME") {
    return new Intl.DateTimeFormat("en-US", {
      timeZone: EASTERN_TIME_ZONE,
      hour: "numeric",
      minute: "2-digit",
    })
      .format(date)
      .toUpperCase();
  }
  if (format === "DAY_DATE") {
    return new Intl.DateTimeFormat("en-US", {
      timeZone: EASTERN_TIME_ZONE,
      weekday: "short",
      month: "2-digit",
      day: "2-digit",
    })
      .format(date)
      .replace(",", "")
      .toUpperCase();
  }
  if (format === "DAY_TIME") {
    return new Intl.DateTimeFormat("en-US", {
      timeZone: EASTERN_TIME_ZONE,
      weekday: "short",
      hour: "numeric",
      minute: "2-digit",
    })
      .format(date)
      .replace(",", "")
      .toUpperCase();
  }
  if (format === "FULL_TIME") {
    return new Intl.DateTimeFormat("en-US", {
      timeZone: EASTERN_TIME_ZONE,
      month: "short",
      day: "2-digit",
      hour: "numeric",
      minute: "2-digit",
    })
      .format(date)
      .replace(",", "")
      .toUpperCase();
  }
  if (format === "YEAR_TIME") {
    return new Intl.DateTimeFormat("en-US", {
      timeZone: EASTERN_TIME_ZONE,
      year: "2-digit",
      month: "short",
      day: "2-digit",
      hour: "numeric",
      minute: "2-digit",
    })
      .format(date)
      .replace(/,/g, "")
      .toUpperCase();
  }
  if (format === "SECOND_TIME") {
    return new Intl.DateTimeFormat("en-US", {
      timeZone: EASTERN_TIME_ZONE,
      hour: "numeric",
      minute: "2-digit",
      second: "2-digit",
    })
      .format(date)
      .toUpperCase();
  }
  if (format === "YEAR_SECOND_TIME") {
    return new Intl.DateTimeFormat("en-US", {
      timeZone: EASTERN_TIME_ZONE,
      year: "2-digit",
      month: "short",
      day: "2-digit",
      hour: "numeric",
      minute: "2-digit",
      second: "2-digit",
    })
      .format(date)
      .replace(/,/g, "")
      .toUpperCase();
  }
  return new Intl.DateTimeFormat("en-US", {
    timeZone: EASTERN_TIME_ZONE,
    month: "short",
    day: "2-digit",
  })
    .format(date)
    .toUpperCase();
}

function formatChartTickTitle(time: number): string {
  return new Intl.DateTimeFormat("en-US", {
    timeZone: EASTERN_TIME_ZONE,
    month: "short",
    day: "2-digit",
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
  }).format(new Date(time));
}

function primaryChartTickFormat(range: ChartRange, spanMs: number): ChartTickFormat {
  if (range === "TODAY" || range === "1D") {
    return "TIME";
  }
  if (range === "1W") {
    return "DAY_DATE";
  }
  if (range === "1M") {
    return "MONTH_DAY";
  }
  if (spanMs < 48 * chartHourMs) {
    return "DAY_TIME";
  }
  if (spanMs < 14 * chartDayMs) {
    return "FULL_TIME";
  }
  return "MONTH_DAY";
}

function moreSpecificChartTickFormat(format: ChartTickFormat): ChartTickFormat {
  if (format === "TIME") {
    return "DAY_TIME";
  }
  if (format === "DAY_DATE" || format === "MONTH_DAY") {
    return "FULL_TIME";
  }
  if (format === "DAY_TIME") {
    return "SECOND_TIME";
  }
  if (format === "FULL_TIME") {
    return "YEAR_TIME";
  }
  return "YEAR_SECOND_TIME";
}

function hasDuplicateChartLabels(labels: string[]): boolean {
  return new Set(labels).size !== labels.length;
}

function chartXTicks(domain: ChartDomain, range: ChartRange) {
  const count = 7;
  const span = domain.end - domain.start || 1;
  const times = Array.from({ length: count }, (_, index) => domain.start + (span * index) / (count - 1));
  let format = primaryChartTickFormat(range, span);
  let labels = times.map((time) => formatChartTick(time, format));

  if (hasDuplicateChartLabels(labels)) {
    format = moreSpecificChartTickFormat(format);
    labels = times.map((time) => formatChartTick(time, format));
  }
  if (hasDuplicateChartLabels(labels)) {
    format = moreSpecificChartTickFormat(format);
    labels = times.map((time) => formatChartTick(time, format));
  }

  return times.map((time, index) => ({
    time,
    label: labels[index],
    title: formatChartTickTitle(time),
  }));
}

function buildChart(series: ChartDataPoint[], mode: ChartMode, domain: ChartDomain, range: ChartRange) {
  const width = 1200;
  const height = 260;
  const padding = { top: 24, right: 28, bottom: 34, left: 54 };
  const values = series.map((point) => point.value);
  const min = values.length ? Math.min(...values) : mode === "VALUE" ? 24 : -1;
  const max = values.length ? Math.max(...values) : mode === "VALUE" ? 40 : 1;
  const paddingValue = mode === "P/L %" ? 0.005 : mode === "P/L $" ? 1 : 2;
  const yMin = Math.floor((min - paddingValue) / paddingValue) * paddingValue;
  const yMax = Math.ceil((max + paddingValue) / paddingValue) * paddingValue;
  const yRange = yMax - yMin || 1;
  const plotLeft = padding.left;
  const plotRight = width - padding.right;
  const xRange = plotRight - plotLeft;
  const yPixels = height - padding.top - padding.bottom;
  const domainSpan = domain.end - domain.start || 1;

  const points = series.map((point) => {
    const x = plotLeft + ((point.time - domain.start) / domainSpan) * xRange;
    const y = padding.top + ((yMax - point.value) / yRange) * yPixels;
    return { ...point, x, y };
  });
  const polylines: string[] = [];
  const singlePoints: { x: number; y: number }[] = [];
  let segment: string[] = [];

  for (const point of points) {
    if (point.breakBefore && segment.length > 0) {
      if (segment.length === 1) {
        const [x, y] = segment[0].split(",").map(Number);
        singlePoints.push({ x, y });
      } else {
        polylines.push(segment.join(" "));
      }
      segment = [];
    }
    segment.push(`${point.x.toFixed(2)},${point.y.toFixed(2)}`);
  }

  if (segment.length === 1) {
    const [x, y] = segment[0].split(",").map(Number);
    singlePoints.push({ x, y });
  } else if (segment.length > 1) {
    polylines.push(segment.join(" "));
  }

  return {
    width,
    height,
    yMin,
    yMax,
    points,
    polylines,
    singlePoints,
    plotLeft,
    plotRight,
    latest: points[points.length - 1],
    xTicks: chartXTicks(domain, range),
  };
}

function PortfolioChart({ summary }: { summary: DashboardSummary }) {
  const [activeRange, setActiveRange] = useState<ChartRange>("TODAY");
  const [activeMode, setActiveMode] = useState<ChartMode>("VALUE");
  const [nowMs, setNowMs] = useState<number | null>(null);

  useEffect(() => {
    const updateClock = () => setNowMs(Date.now());
    updateClock();
    const timer = window.setInterval(updateClock, 60_000);
    return () => window.clearInterval(timer);
  }, []);

  const startingBalance = summary.active_epoch?.starting_balance ?? summary.paper_starting_balance ?? summary.portfolio_series[0]?.value ?? 500;
  const chartSeries = useMemo(
    () =>
      nowMs === null
        ? {
            domain: { start: 0, end: 1 },
            series: [],
            limitedData: false,
            historyTruncated: false,
          }
        : portfolioChartSeries(summary, activeRange, startingBalance, nowMs),
    [activeRange, nowMs, startingBalance, summary],
  );
  const displaySeries = useMemo(
    () => transformPortfolioSeries(chartSeries.series, activeMode, startingBalance),
    [activeMode, chartSeries.series, startingBalance],
  );
  const chart = buildChart(displaySeries, activeMode, chartSeries.domain, activeRange);
  const latestSnapshotValue = summary.portfolio_series[summary.portfolio_series.length - 1]?.value;
  const latestRaw = summary.portfolio_value ?? latestSnapshotValue ?? null;
  const latest =
    activeMode === "VALUE"
      ? latestRaw
      : latestRaw !== null && startingBalance > 0
        ? activeMode === "P/L $"
          ? latestRaw - startingBalance
          : (latestRaw - startingBalance) / startingBalance
        : null;
  const changePct = latestRaw !== null && startingBalance > 0 ? (latestRaw - startingBalance) / startingBalance : null;
  const ticks = [chart.yMax, (chart.yMax + chart.yMin) / 2, chart.yMin];
  const title =
    activeMode === "VALUE"
      ? "PORTFOLIO VALUE (PAPER TRADING)"
      : activeMode === "P/L $"
        ? "PORTFOLIO P/L $ (PAPER TRADING)"
        : "PORTFOLIO P/L % (PAPER TRADING)";

  return (
    <section className="panel chart-panel">
      <div className="panel-heading chart-heading">
        <div>
          <h2>{title}</h2>
          <div className="chart-value-row">
            <strong>{formatChartValue(latest, activeMode)}</strong>
            <span className={pctClass(changePct)}>{formatPercent(changePct)}</span>
          </div>
        </div>
        <div className="chart-control-stack">
          <div className="chart-controls" aria-label="Portfolio chart range controls">
            {chartRanges.map((control) => (
              <button
                key={control}
                className={control === activeRange ? "active" : ""}
                type="button"
                onClick={() => setActiveRange(control)}
              >
                {chartRangeLabels[control]}
              </button>
            ))}
          </div>
          <div className="chart-controls" aria-label="Portfolio chart display mode controls">
            {chartModes.map((control) => (
              <button
                key={control}
                className={control === activeMode ? "active" : ""}
                type="button"
                onClick={() => setActiveMode(control)}
              >
                {chartModeLabels[control]}
              </button>
            ))}
          </div>
          {chartSeries.historyTruncated ? (
            <span className="chart-limited-state">SHOWING RETURNED SNAPSHOT WINDOW</span>
          ) : chartSeries.limitedData ? (
            <span className="chart-limited-state">CARRY-FORWARD VALUE FOR {chartRangeLabels[activeRange]}</span>
          ) : null}
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
          {displaySeries.length > 0 ? (
            <>
              {chart.polylines.map((polyline) => (
                <polyline key={polyline} points={polyline} />
              ))}
              {chart.singlePoints.map((point) => (
                <circle key={`${point.x}-${point.y}`} cx={point.x} cy={point.y} r="3" className="chart-anchor-point" />
              ))}
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
            <span key={tick}>{formatChartValue(tick, activeMode)}</span>
          ))}
        </div>
        <div className="chart-x-axis">
          {nowMs === null ? null : chart.xTicks.map((tick) => (
            <span key={`${activeRange}-${tick.time}`} title={tick.title}>
              {tick.label}
            </span>
          ))}
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
              <th>LAST MARK TIME</th>
              <th>QTY</th>
              <th>P/L ($)</th>
              <th>P/L (%)</th>
              <th>GAME STATUS</th>
              <th>STATUS</th>
              <th>RESOLUTION</th>
            </tr>
          </thead>
          <tbody>
            {positions.length === 0 ? (
              <tr>
                <td colSpan={12} className="table-empty">
                  <b>NO OPEN POSITIONS</b>
                  <span>PAPER TRADING HAS NOT TAKEN ANY POSITIONS YET.</span>
                </td>
              </tr>
            ) : (
              positions.map((position) => (
                <tr key={`${position.market}-${position.side}-${position.time_entered}-${position.entry_price}`}>
                  <td>{position.time_entered_display ?? formatEastern(position.time_entered)}</td>
                  <td>
                    <span className="market-primary">
                      {position.matchup_display ? `${position.matchup_display} · ` : ""}
                      {position.contract_display ?? position.market}
                    </span>
                    {position.normalized_equivalent_display ? (
                      <span className="market-secondary">{position.normalized_equivalent_display}</span>
                    ) : null}
                    {positionRationaleLabel(position) ? (
                      <span className="market-secondary">{positionRationaleLabel(position)}</span>
                    ) : null}
                    {position.display_title ? <span className="market-secondary">{position.display_title}</span> : null}
                    {position.display_subtitle ? <span className="market-secondary">{position.display_subtitle}</span> : null}
                    <span
                      className="market-secondary"
                      title={position.raw_ticker_display ?? position.market_ticker ?? position.market}
                    >
                      {position.raw_ticker_display ?? position.market_ticker ?? position.market}
                    </span>
                  </td>
                  <td>{position.side.toUpperCase()}</td>
                  <td>{formatPrice(position.entry_price)}</td>
                  <td>{formatPrice(position.current_price)}</td>
                  <td>{position.current_price_updated_at_display ?? formatEastern(position.current_price_updated_at)}</td>
                  <td>{position.quantity}</td>
                  <td className={pctClass(position.profit_loss)}>{formatSignedCurrency(position.profit_loss)}</td>
                  <td className={pctClass(position.profit_loss_percent)}>{formatPercent(position.profit_loss_percent)}</td>
                  <td>{position.game_status_display ?? position.game_status ?? "UNKNOWN"}</td>
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

function ClosedPositionsTable({
  positions,
  selectedDate,
  onSelectedDateChange,
}: {
  positions: PositionSummary[];
  selectedDate: string;
  onSelectedDateChange: (value: string) => void;
}) {
  const [pickerOpen, setPickerOpen] = useState(false);
  return (
    <section className="panel positions-panel">
      <div className="panel-heading positions-heading">
        <h2>CLOSED POSITIONS</h2>
        <div className="closed-position-controls">
          <button type="button" onClick={() => onSelectedDateChange(shiftDate(selectedDate, -1))}>
            PREVIOUS
          </button>
          <button type="button" onClick={() => onSelectedDateChange(easternDateString())}>
            TODAY
          </button>
          <button type="button" onClick={() => onSelectedDateChange(shiftDate(selectedDate, 1))}>
            TOMORROW
          </button>
          <button type="button" onClick={() => setPickerOpen((open) => !open)}>
            {formatDateButton(selectedDate)}
          </button>
          <span>{positions.length} CLOSED POSITIONS</span>
          {pickerOpen ? (
            <input
              aria-label="Closed positions date"
              type="date"
              value={selectedDate}
              onChange={(event) => {
                onSelectedDateChange(event.target.value);
                setPickerOpen(false);
              }}
            />
          ) : null}
        </div>
      </div>
      <div className="terminal-table-wrap">
        <table className="terminal-table">
          <thead>
            <tr>
              <th>TIME ENTERED (EDT/EST)</th>
              <th>TIME CLOSED (EDT/EST)</th>
              <th>MARKET</th>
              <th>SIDE</th>
              <th>ENTRY PRICE</th>
              <th>EXIT PRICE</th>
              <th>QTY</th>
              <th>P/L ($)</th>
              <th>P/L (%)</th>
              <th>GAME STATUS</th>
              <th>STATUS</th>
              <th>RESOLUTION</th>
            </tr>
          </thead>
          <tbody>
            {positions.length === 0 ? (
              <tr>
                <td colSpan={12} className="table-empty">
                  <b>NO CLOSED POSITIONS FOR SELECTED DATE</b>
                  <span>{formatDateButton(selectedDate)}</span>
                </td>
              </tr>
            ) : (
              positions.map((position) => (
                <tr key={`${position.market}-${position.side}-${position.time_entered}-${position.time_closed}`}>
                  <td>{position.time_entered_display ?? formatEastern(position.time_entered)}</td>
                  <td>{position.time_closed_display ?? formatEastern(position.time_closed)}</td>
                  <td>
                    <span className="market-primary">
                      {position.matchup_display ? `${position.matchup_display} · ` : ""}
                      {position.contract_display ?? position.market}
                    </span>
                    {position.normalized_equivalent_display ? (
                      <span className="market-secondary">{position.normalized_equivalent_display}</span>
                    ) : null}
                    {positionRationaleLabel(position) ? (
                      <span className="market-secondary">{positionRationaleLabel(position)}</span>
                    ) : null}
                    {position.display_title ? <span className="market-secondary">{position.display_title}</span> : null}
                    {position.display_subtitle ? <span className="market-secondary">{position.display_subtitle}</span> : null}
                    <span
                      className="market-secondary"
                      title={position.raw_ticker_display ?? position.market_ticker ?? position.market}
                    >
                      {position.raw_ticker_display ?? position.market_ticker ?? position.market}
                    </span>
                  </td>
                  <td>{position.side.toUpperCase()}</td>
                  <td>{formatPrice(position.entry_price)}</td>
                  <td>{formatPrice(position.exit_price)}</td>
                  <td>{position.quantity}</td>
                  <td className={pctClass(position.profit_loss)}>{formatSignedCurrency(position.profit_loss)}</td>
                  <td className={pctClass(position.profit_loss_percent)}>{formatPercent(position.profit_loss_percent)}</td>
                  <td>{position.game_status_display ?? position.game_status ?? "UNKNOWN"}</td>
                  <td>{position.status}</td>
                  <td>{position.resolution ?? position.outcome ?? "PENDING"}</td>
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

function PerformanceBreakdownTable({
  title,
  rows,
  decisions,
}: {
  title: string;
  rows: Record<string, Record<string, unknown>>;
  decisions: Record<string, Record<string, number>>;
}) {
  const keys = Array.from(new Set([...Object.keys(rows), ...Object.keys(decisions)])).sort();
  return (
    <section className="panel positions-panel">
      <div className="panel-heading positions-heading">
        <h2>{title}</h2>
        <span>{keys.length} GROUPS</span>
      </div>
      <div className="terminal-table-wrap">
        <table className="terminal-table compact-table">
          <thead>
            <tr>
              <th>GROUP</th>
              <th>TRADES</th>
              <th>WIN RATE</th>
              <th>ROI</th>
              <th>P/L</th>
              <th>RECORD</th>
              <th>CANDIDATES</th>
              <th>PAPER TRADES</th>
            </tr>
          </thead>
          <tbody>
            {keys.length === 0 ? (
              <tr>
                <td colSpan={8} className="table-empty">
                  <b>NO PERFORMANCE GROUPS</b>
                  <span>ACTIVE EPOCH HAS NO SETTLED PAPER SAMPLE YET.</span>
                </td>
              </tr>
            ) : (
              keys.map((key) => {
                const row = rows[key] ?? {};
                const decision = decisions[key] ?? {};
                const candidateCount = Object.values(decision).reduce((sum, value) => sum + value, 0);
                return (
                  <tr key={key}>
                    <td>{key.replaceAll("_", " ").toUpperCase()}</td>
                    <td>{formatUnknown(row.trades)}</td>
                    <td>{formatPercent(typeof row.win_rate === "number" ? row.win_rate : null)}</td>
                    <td>{formatPercent(typeof row.roi === "number" ? row.roi : null)}</td>
                    <td className={pctClass(typeof row.profit_loss === "number" ? row.profit_loss : null)}>
                      {formatSignedCurrency(typeof row.profit_loss === "number" ? row.profit_loss : null)}
                    </td>
                    <td>{typeof row.record === "string" ? row.record : "0-0-0"}</td>
                    <td>{candidateCount}</td>
                    <td>{decision.paper_trade ?? 0}</td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function jobStatusRows(summary: DashboardSummary): StatusRow[] {
  const jobLabels: [string, string][] = [
    ["LAST SETUP JOB", "daily-setup"],
    ["LAST CANDIDATE SWEEP", "candidate-sweep"],
    ["LAST PRICE REFRESH", "price-refresh"],
    ["LAST SETTLEMENT", "settlement"],
    ["LAST GOVERNANCE", "governance"],
    ["FULL PAPER CYCLE", "full-paper-cycle"],
    ["SPREAD AUDIT", "spread-audit"],
  ];
  const rows = jobLabels.map(([label, key]) => {
    const job = summary.job_status[key];
    const value = job ? `${job.status.toUpperCase()} ${job.completed_at ? formatEastern(job.completed_at) : ""}`.trim() : "NOT RUN";
    return {
      label,
      value,
      tone: job?.status === "succeeded" ? "green" : job?.status === "failed" ? "red" : "amber",
    } as StatusRow;
  });
  const ws = summary.websocket_status;
  rows.push({
    label: "WS STATUS",
    value: ws ? `${ws.running ? "RUNNING" : ws.enabled ? "ENABLED" : "REST FALLBACK"} / ${ws.source.toUpperCase()}` : "REST FALLBACK",
    tone: ws?.running ? "green" : ws?.enabled ? "amber" : "amber",
  });
  rows.push({
    label: "WS MARKETS",
    value: String(ws?.subscribed_market_count ?? 0),
    tone: (ws?.stale_count ?? 0) > 0 ? "amber" : "green",
  });
  return rows;
}

function modelNotes(notes: string | string[]): string {
  return Array.isArray(notes) ? notes.join(" ") : notes;
}

export default function DashboardPage() {
  const apiBaseUrl = useMemo(() => getApiBaseUrl(), []);
  const refreshMs = useMemo(() => getRefreshMs(), []);
  const [selectedClosedDate, setSelectedClosedDate] = useState<string>(() => easternDateString());
  const [state, setState] = useState<DashboardState>({ status: "loading" });

  const loadDashboard = useCallback(async () => {
    try {
      const [summary, system] = await Promise.all([
        fetchJson<DashboardSummary>(`${apiBaseUrl}/v1/dashboard/summary?closed_date=${selectedClosedDate}`),
        fetchJson<SystemStatus>(`${apiBaseUrl}/v1/system/status`),
      ]);
      setState({ status: "ready", summary, system });
    } catch (error) {
      const detail = error instanceof Error ? error.message : "UNKNOWN ERROR";
      setState((previous) => ({
        status: "error",
        message: `API UNAVAILABLE AT ${apiBaseUrl}: ${detail}`,
        summary: previous.status === "loading" ? null : previous.summary,
        system: previous.status === "loading" ? null : previous.system,
      }));
    }
  }, [apiBaseUrl, selectedClosedDate]);

  useEffect(() => {
    loadDashboard();
    const timer = window.setInterval(loadDashboard, refreshMs);
    return () => window.clearInterval(timer);
  }, [loadDashboard, refreshMs]);

  const summary = state.status === "ready" ? state.summary : state.status === "error" && state.summary ? state.summary : emptySummary;
  const system = state.status === "ready" ? state.system : state.status === "error" ? state.system : null;
  const connected = state.status === "ready";
  const tradeCapsUsed = summary.model_status.trade_caps_used;
  const sweepWindowEnabled = tradeCapsUsed.sweep_window_enabled === true;
  const sweepLabel = typeof tradeCapsUsed.sweep_label === "string" && tradeCapsUsed.sweep_label ? tradeCapsUsed.sweep_label : "manual";
  const sweepMin = tradeCapsUsed.min_time_to_start_minutes ?? "ANY";
  const sweepMax = tradeCapsUsed.max_time_to_start_minutes ?? "ANY";
  const sweepWindowLabel = sweepWindowEnabled
    ? `${formatUnknown(sweepLabel)} ${formatUnknown(sweepMin)}-${formatUnknown(sweepMax)} MIN`
    : "NO WINDOW";
  const riskBasisType =
    typeof tradeCapsUsed.risk_limit_basis_type === "string" ? tradeCapsUsed.risk_limit_basis_type : "UNKNOWN";
  const riskBasisAmount =
    typeof tradeCapsUsed.risk_limit_basis_amount === "number" ? formatCurrency(tradeCapsUsed.risk_limit_basis_amount) : "N/A";

  const modelRows: StatusRow[] = [
    { label: "ACTIVE MODEL VERSION", value: summary.model_status.active_model_version ?? "NONE" },
    { label: "ACTIVE PARAMETER VERSION", value: summary.model_status.active_parameter_version ?? "NONE" },
    { label: "FEATURE VERSION", value: summary.model_status.feature_version ?? "NONE" },
    {
      label: "FEATURE COMPLETENESS",
      value: featureCompletenessLabel(summary.model_status.feature_completeness),
      tone: summary.model_status.critical_module_warnings.length ? "amber" : "green",
    },
    {
      label: "CRITICAL MODULE WARNINGS",
      value: summary.model_status.critical_module_warnings.length
        ? String(summary.model_status.critical_module_warnings.length)
        : "0",
      tone: summary.model_status.critical_module_warnings.length ? "amber" : "green",
    },
    { label: "LINEUP STATUS", value: (summary.model_status.lineup_status ?? "MISSING").toUpperCase() },
    { label: "STARTER STATUS", value: (summary.model_status.starter_status ?? "MISSING").toUpperCase() },
    { label: "WEATHER STATUS", value: (summary.model_status.weather_status ?? "MISSING").toUpperCase() },
    {
      label: "CALIBRATION STATUS",
      value: summary.model_status.calibration_status ?? "NOT RUN",
      tone: summary.model_status.calibration_status === "calibrated" ? "green" : "amber",
    },
    { label: "CANDIDATES", value: String(summary.model_status.candidate_count) },
    { label: "TRAINING ELIGIBLE", value: String(summary.model_status.training_eligible_count) },
    { label: "RESOLVED MATURE SAMPLES", value: String(summary.model_status.resolved_mature_samples) },
    { label: "DATA QUALITY AVG", value: formatNumber(summary.model_status.data_quality_summary.avg ?? null) },
    {
      label: "TRADES USED / MAX TODAY",
      value: `${formatUnknown(tradeCapsUsed.paper_trades)} / ${formatUnknown(
        summary.model_status.trade_policy.paper_max_trades_per_slate,
      )}`,
    },
    {
      label: "GAME SCOPE CAP",
      value: `${formatUnknown(tradeCapsUsed.game_scope_correlation_candidates_kept)} KEPT / ${formatUnknown(
        tradeCapsUsed.game_scope_correlation_candidates_rejected,
      )} BLOCKED`,
    },
    { label: "LAST CANDIDATE SWEEP WINDOW", value: sweepWindowLabel, tone: sweepWindowEnabled ? "green" : "amber" },
    { label: "GAMES IN WINDOW", value: formatUnknown(tradeCapsUsed.games_in_window) },
    {
      label: "GAMES EXCLUDED TOO EARLY/LATE/STARTED",
      value: `${formatUnknown(tradeCapsUsed.games_excluded_too_soon)} / ${formatUnknown(
        tradeCapsUsed.games_excluded_too_late,
      )} / ${formatUnknown(tradeCapsUsed.games_excluded_started)}`,
    },
    { label: "NEXT ELIGIBLE GAME", value: formatEastern(tradeCapsUsed.next_game_in_window_start_time_et as string | null | undefined) },
    { label: "PAPER TRADES CREATED IN LAST SWEEP", value: formatUnknown(tradeCapsUsed.paper_trades_in_window) },
    {
      label: "YES / NO CANDIDATES",
      value: `${formatUnknown(tradeCapsUsed.candidates_yes)} / ${formatUnknown(
        tradeCapsUsed.candidates_no,
      )}`,
    },
    {
      label: "SPREAD TRADING",
      value: summary.model_status.trade_policy.paper_spread_trading_enabled ? "ENABLED" : "DISABLED",
      tone: summary.model_status.trade_policy.paper_spread_trading_enabled ? "amber" : "green",
    },
    {
      label: "SIDE-AWARE CANDIDATES",
      value: summary.model_status.trade_policy.side_aware_candidates_enabled === false ? "DISABLED" : "ENABLED",
      tone: summary.model_status.trade_policy.side_aware_candidates_enabled === false ? "red" : "green",
    },
    {
      label: "RISK CAPS",
      value: summary.model_status.trade_policy.aggregate_risk_caps_enabled === false ? "DISABLED" : "ENABLED",
      tone: summary.model_status.trade_policy.aggregate_risk_caps_enabled === false ? "red" : "green",
    },
    { label: "RISK LIMIT BASIS", value: `${riskBasisType.toUpperCase()} ${riskBasisAmount}` },
    {
      label: "DAILY RISK USED / MAX",
      value: `${formatUnknown(tradeCapsUsed.daily_risk_used)} / ${formatUnknown(
        tradeCapsUsed.daily_risk_max,
      )}`,
    },
    {
      label: "OPEN RISK USED / MAX",
      value: `${formatUnknown(tradeCapsUsed.open_risk_used)} / ${formatUnknown(
        tradeCapsUsed.open_risk_max,
      )}`,
    },
    { label: "GOVERNANCE STATUS", value: summary.model_status.governance_status ?? "NOT RUN" },
    { label: "LAST GOVERNANCE", value: summary.model_status.last_governance_status ?? "NOT RUN" },
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

      {summary.model_status.calibration_status !== "calibrated" ? (
        <section className="model-warning" role="status">
          MODEL UNCALIBRATED OR SAMPLE-LIMITED: PAPER TRADES USE STRICT PR3C CAPS AND CONSERVATIVE SHRINKAGE.
        </section>
      ) : null}

      <PortfolioChart summary={summary} />

      <section className="metrics-grid" aria-label="Performance metrics">
        <MetricCard label="WIN RATE" value={formatPercent(summary.performance.win_rate)} detail="SETTLED PAPER TRADES" />
        <MetricCard label="ROI" value={formatPercent(summary.performance.roi)} detail="HOLD-TO-SETTLEMENT BASIS" />
        <MetricCard label="P/L" value={formatSignedCurrency(summary.performance.profit_loss)} detail="PAPER TRADING ONLY" />
        <MetricCard label="RECORD" value={summary.performance.record} detail="WINS-LOSSES-PUSHES" />
      </section>

      <PositionsTable positions={summary.positions} />
      <ClosedPositionsTable
        positions={summary.closed_positions}
        selectedDate={summary.closed_positions_date ?? selectedClosedDate}
        onSelectedDateChange={setSelectedClosedDate}
      />

      <section className="breakdown-grid">
        <PerformanceBreakdownTable
          title="PERFORMANCE BY SCOPE"
          rows={summary.performance_by_scope}
          decisions={summary.decision_breakdown_by_scope}
        />
        <PerformanceBreakdownTable
          title="PERFORMANCE BY FAMILY"
          rows={summary.performance_by_family}
          decisions={summary.decision_breakdown_by_family}
        />
      </section>

      <section className="status-grid">
        <StatusTable title="MODEL QUALITY" rows={modelRows} note={modelNotes(summary.model_status.notes)} />
        <StatusTable title="PAPER OPS JOBS" rows={jobStatusRows(summary)} note="JOBS ARE API OR CRON RUNS; RESET IS PROTECTED API ONLY." />
        <StatusTable title="SYSTEM STATUS" rows={systemRows} note={system?.database.message ?? "DATABASE STATUS UNAVAILABLE."} />
      </section>

      <footer className="terminal-footer">
        <span>ALL TIMES DISPLAYED IN EDT/EST</span>
        <span>HOMERUN V0.3.2 | PR3C MATURE MODEL GOVERNANCE</span>
      </footer>
    </main>
  );
}
