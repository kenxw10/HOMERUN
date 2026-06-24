from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


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


class PositionSummary(BaseModel):
    market: str
    side: Literal["yes", "no"]
    entry_price: float
    current_price: float | None
    quantity: int
    status: str
    resolution: str | None


class BotMode(BaseModel):
    mode: Literal["paper"]
    paper_trading: bool
    live_trading_enabled: bool
    execution_kill_switch: bool
    kalshi_env: str


class ModelStatus(BaseModel):
    active_model_version: str | None
    last_training_run: datetime | None
    last_calibration_run: datetime | None
    candidate_count: int
    notes: str


class DashboardSummary(BaseModel):
    portfolio_series: list[PortfolioPoint]
    performance: PerformanceMetrics
    positions: list[PositionSummary]
    bot: BotMode
    model_status: ModelStatus


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
    kalshi_credentials: Literal["not_set", "set_redacted"]


class SystemStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: BackendStatus
    database: DatabaseStatus
    config: ConfigStatus
