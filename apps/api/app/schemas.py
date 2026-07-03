from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    app_env: str
    paper_trading: bool
    live_trading_enabled: bool
    timestamp: datetime


class PortfolioPoint(BaseModel):
    timestamp: datetime
    value: float


class PerformanceMetrics(BaseModel):
    win_rate: float | None
    roi: float | None
    profit_loss: float
    record: str


class ActiveEpochSummary(BaseModel):
    epoch_key: str
    display_name: str
    status: str
    mode: str
    starting_balance: float
    started_at: str | None = None


class JobRunSummary(BaseModel):
    job_name: str
    status: str
    started_at: str | None = None
    completed_at: str | None = None
    duration_seconds: int | None = None
    target_date: str | None = None
    result_is_compact: bool = True
    step_count: int | None = None
    warning_count: int | None = None
    error_count: int | None = None
    result: dict[str, object] = Field(default_factory=dict)


class ObservationFilterSummary(BaseModel):
    active: bool
    include_pre_observation: bool
    observation_start_date: str
    observation_start_at: str
    observation_start_display: str
    excluded_pre_observation_count: int = 0
    excluded_pre_observation_closed_count: int = 0
    historical_rows_available: bool = False
    history_param: str = "include_pre_observation=true"
    reason: str


class WebSocketStatusSummary(BaseModel):
    enabled: bool
    running: bool
    source: str
    subscribed_market_count: int = 0
    last_seen_at: str | None = None
    last_message_at: str | None = None
    reconnect_count: int = 0
    stale_count: int = 0
    last_error: str | None = None


class PositionSummary(BaseModel):
    time_entered: str | None = None
    time_entered_display: str | None = None
    time_closed: str | None = None
    time_closed_display: str | None = None
    market: str
    market_ticker: str | None = None
    market_display: str | None = None
    selection_display: str | None = None
    matchup_display: str | None = None
    contract_display: str | None = None
    normalized_equivalent_display: str | None = None
    display_title: str | None = None
    display_subtitle: str | None = None
    raw_ticker_display: str | None = None
    selected_position_rationale: dict[str, object] = Field(default_factory=dict)
    side: Literal["yes", "no"]
    entry_price: float
    exit_price: float | None = None
    current_price: float | None
    entry_notional: float | None = None
    entry_total_cost: float | None = None
    current_value: float | None = None
    exit_value: float | None = None
    fee_paid: float | None = None
    estimated_fee: float | None = None
    current_price_updated_at: str | None = None
    current_price_updated_at_display: str | None = None
    quantity: int
    profit_loss: float | None = None
    profit_loss_percent: float | None = None
    status: str
    game_status: str | None = None
    game_status_display: str | None = None
    resolution: str | None
    outcome: str | None = None


class BotMode(BaseModel):
    mode: Literal["paper"]
    paper_trading: bool
    live_trading_enabled: bool
    execution_kill_switch: bool
    kalshi_env: str


class ModelStatus(BaseModel):
    active_model_version: str | None
    active_parameter_version: str | None = None
    active_calibration_version: str | None = None
    feature_version: str | None = None
    calibration_status: str | None = None
    last_training_run: datetime | None
    last_calibration_run: datetime | None
    candidate_count: int
    resolved_mature_samples: int = 0
    raw_resolved_mature_samples: int = 0
    clean_resolved_mature_samples: int = 0
    pre_clean_excluded_samples: int = 0
    training_eligible_count: int = 0
    clean_training_eligible_count: int = 0
    last_governance_status: str | None = None
    governance_training_policy: str | None = None
    clean_training_start_at: str | None = None
    clean_training_start_at_et: str | None = None
    clean_training_start_date_et: str | None = None
    clean_filter_exclusion_counts: dict[str, int] = Field(default_factory=dict)
    ignored_pre_clean_artifacts: dict[str, object] = Field(default_factory=dict)
    governance_parameter_registry: dict[str, object] = Field(default_factory=dict)
    trade_policy: dict[str, object] = Field(default_factory=dict)
    trade_caps_used: dict[str, object] = Field(default_factory=dict)
    trade_threshold_policy: dict[str, object] = Field(default_factory=dict)
    data_quality_summary: dict[str, object] = Field(default_factory=dict)
    feature_completeness: dict[str, object] = Field(default_factory=dict)
    source_statuses: dict[str, object] = Field(default_factory=dict)
    critical_module_warnings: list[str] = Field(default_factory=list)
    lineup_status: str | None = None
    starter_status: str | None = None
    weather_status: str | None = None
    network_sources_enabled: bool = False
    public_sources_enabled: bool = False
    last_feature_sync_status: dict[str, object] = Field(default_factory=dict)
    source_details: dict[str, object] = Field(default_factory=dict)
    governance_status: str | None = None
    notes: str | list[str]


class DashboardSummary(BaseModel):
    active_epoch: ActiveEpochSummary | None = None
    portfolio_series: list[PortfolioPoint]
    performance: PerformanceMetrics
    positions: list[PositionSummary]
    closed_positions: list[PositionSummary] = Field(default_factory=list)
    closed_positions_date: str | None = None
    closed_positions_count: int = 0
    bot: BotMode
    model_status: ModelStatus
    cash_balance: float | None = None
    portfolio_value: float | None = None
    paper_starting_balance: float | None = None
    observation_filter: ObservationFilterSummary | None = None
    performance_by_scope: dict[str, dict[str, object]] = Field(default_factory=dict)
    performance_by_family: dict[str, dict[str, object]] = Field(default_factory=dict)
    decision_breakdown_by_scope: dict[str, dict[str, int]] = Field(default_factory=dict)
    decision_breakdown_by_family: dict[str, dict[str, int]] = Field(default_factory=dict)
    latest_candidate_diagnostics: dict[str, object] = Field(default_factory=dict)
    job_status: dict[str, JobRunSummary] = Field(default_factory=dict)
    websocket_status: WebSocketStatusSummary | None = None
    last_update: str | None = None
    last_update_display: str | None = None


class BackendStatus(BaseModel):
    ready: bool
    service: str
    app_env: str


class DatabaseStatus(BaseModel):
    ready: bool
    configured: bool
    dialect: str | None
    message: str


class ConfigStatus(BaseModel):
    ready: bool
    paper_trading: bool
    live_trading_enabled: bool
    execution_kill_switch: bool
    kalshi_env: str
    kalshi_market_data_source: str
    kalshi_market_data_base_kind: str
    kalshi_credentials: Literal["not_set", "set_redacted"]
    feature_sync_enable_network_sources: bool = False
    public_sources_enabled: bool = False
    source_status: dict[str, object] = Field(default_factory=dict)


class SystemStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: BackendStatus
    database: DatabaseStatus
    config: ConfigStatus


class GameSummary(BaseModel):
    external_game_id: str
    home_team: str
    away_team: str
    scheduled_start: str | None
    scheduled_start_display: str | None
    status: str
    home_score: int | None
    away_score: int | None


class MarketMappingSummary(BaseModel):
    mapping_status: str | None
    confidence: float | None
    rationale: str | None
    metadata: dict[str, object] | None


class MarketSummary(BaseModel):
    ticker: str
    event_ticker: str | None
    title: str
    subtitle: str | None
    status: str
    close_time: str | None
    close_time_display: str | None
    best_yes_bid: float | None
    implied_yes_ask: float | None
    best_no_bid: float | None
    implied_no_ask: float | None
    mapping: MarketMappingSummary | None


class CandidateSummary(BaseModel):
    evaluated_at: str | None
    evaluated_at_display: str | None
    game: str | None
    market_ticker: str | None
    market_type: str | None
    contract_side: str | None = None
    contract_display: str | None = None
    normalized_equivalent_display: str | None = None
    display_title: str | None = None
    display_subtitle: str | None = None
    raw_ticker_display: str | None = None
    time_bucket: str | None
    time_to_start_minutes: int | None
    model_probability: float | None
    probability_raw: float | None = None
    probability_calibrated: float | None = None
    executable_price: float | None
    net_expected_value: float | None
    data_quality: float | None = None
    calibration_status: str | None = None
    training_eligible: bool | None = None
    decision: str


class ListResponse(BaseModel):
    items: list[dict[str, object]]
    count: int
    database_ready: bool


class RunResponse(BaseModel):
    ok: bool
    action: str
    result: dict[str, object]


class PaperEpochResetRequest(BaseModel):
    archive_current_as: str = "pre_pr3d_validation"
    new_epoch: str = "pr3d_paper_observation_v1"
    starting_balance: float = 500.0
    archive_open_positions: bool = True
    reset_dashboard_metrics: bool = True
    confirmation: str
