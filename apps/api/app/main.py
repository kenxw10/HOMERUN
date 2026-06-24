from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.database import database_status
from app.schemas import (
    BackendStatus,
    BotMode,
    ConfigStatus,
    DashboardSummary,
    HealthResponse,
    ModelStatus,
    PerformanceMetrics,
    SystemStatus,
)

settings = get_settings()

app = FastAPI(
    title="HOMERUN API",
    version="0.1.0",
    description="Kalshi-native MLB paper-trading backend.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


def utc_now() -> datetime:
    return datetime.now(UTC)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        service=settings.service_name,
        app_env=settings.app_env,
        paper_trading=settings.paper_trading,
        live_trading_enabled=settings.live_trading_enabled,
        timestamp=utc_now(),
    )


@app.get("/v1/dashboard/summary", response_model=DashboardSummary)
def dashboard_summary() -> DashboardSummary:
    return DashboardSummary(
        portfolio_series=[],
        performance=PerformanceMetrics(
            win_rate=None,
            roi=None,
            profit_loss=0.0,
            record="0-0-0",
        ),
        positions=[],
        bot=BotMode(
            mode="paper",
            paper_trading=settings.paper_trading,
            live_trading_enabled=settings.live_trading_enabled,
            execution_kill_switch=settings.execution_kill_switch,
            kalshi_env=settings.kalshi_env,
        ),
        model_status=ModelStatus(
            active_model_version=None,
            last_training_run=None,
            last_calibration_run=None,
            candidate_count=0,
            notes="No model has been trained yet. PR 1 only provides the foundation.",
        ),
    )


@app.get("/v1/system/status", response_model=SystemStatus)
def system_status() -> SystemStatus:
    db_status = database_status()
    credentials_state = "set_redacted" if settings.kalshi_credentials_configured else "not_set"

    return SystemStatus(
        backend=BackendStatus(
            ready=True,
            service=settings.service_name,
            app_env=settings.app_env,
        ),
        database=db_status,
        config=ConfigStatus(
            ready=settings.safe_execution_posture,
            paper_trading=settings.paper_trading,
            live_trading_enabled=settings.live_trading_enabled,
            execution_kill_switch=settings.execution_kill_switch,
            kalshi_env=settings.kalshi_env,
            kalshi_credentials=credentials_state,
        ),
    )
