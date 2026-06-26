from datetime import UTC, date, datetime

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from app.config import get_settings
from app.database import database_status, get_session_factory
from app.models import KalshiMarket, MarketMapping, MlbGame, ModelCandidate, ModelPredictionOutput, ModelPredictionRun
from app.schemas import (
    BackendStatus,
    CandidateSummary,
    ConfigStatus,
    DashboardSummary,
    GameSummary,
    HealthResponse,
    ListResponse,
    MarketMappingSummary,
    MarketSummary,
    RunResponse,
    SystemStatus,
)
from app.security import require_internal_api_key
from app.services.candidates import generate_candidates
from app.services.dashboard import (
    dashboard_summary_from_db,
    empty_dashboard_summary,
    list_today_candidates,
    list_today_games,
    list_today_markets,
)
from app.services.market_family_discovery import (
    latest_market_family_discovery,
    market_family_discovery_preview,
    run_market_family_discovery,
)
from app.services.market_family_mapping import latest_market_family_mapping_report, sync_market_family_mappings
from app.services.features import feature_coverage, sync_mlb_features
from app.services.modeling import governance_status, repair_training_eligibility, run_model_governance
from app.services.market_sync import resolve_preview_for_date, sync_kalshi_markets
from app.services.mlb import sync_results, sync_schedule
from app.services.position_refresh import refresh_open_position_prices
from app.services.portfolio import create_balance_snapshot
from app.services.settlement import settle_paper_trades
from app.time_utils import eastern_display, today_eastern, to_eastern_iso

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
    allow_methods=["GET", "POST"],
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
def dashboard_summary(closed_date: date | None = Query(default=None)) -> DashboardSummary:
    if not database_status()["ready"]:
        return empty_dashboard_summary(closed_date)

    try:
        session_factory = get_session_factory()
        with session_factory() as session:
            return dashboard_summary_from_db(session, closed_date)
    except Exception:
        return empty_dashboard_summary(closed_date)


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
            kalshi_market_data_source=settings.kalshi_market_data_source,
            kalshi_market_data_base_kind=settings.kalshi_market_data_base_kind,
            kalshi_credentials=credentials_state,
        ),
    )


def _db_session_or_503():
    if not database_status()["ready"]:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is not ready. Configure a reachable DATABASE_URL first.",
        )
    try:
        session_factory = get_session_factory()
        return session_factory()
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


def _decimal_float(value) -> float | None:
    return float(value) if value is not None else None


def _prefer_not_none(primary, fallback):
    return primary if primary is not None else fallback


def _game_summary(game: MlbGame) -> GameSummary:
    return GameSummary(
        external_game_id=game.external_game_id,
        home_team=game.home_team,
        away_team=game.away_team,
        scheduled_start=to_eastern_iso(game.scheduled_start),
        scheduled_start_display=eastern_display(game.scheduled_start),
        status=game.status,
        home_score=game.home_score,
        away_score=game.away_score,
    )


def _market_summary(market: KalshiMarket, mapping: MarketMapping | None) -> MarketSummary:
    mapping_summary = None
    if mapping is not None:
        mapping_summary = MarketMappingSummary(
            mapping_status=mapping.mapping_status,
            confidence=_decimal_float(mapping.confidence),
            rationale=mapping.rationale,
            metadata=mapping.mapping_metadata,
        )
    return MarketSummary(
        ticker=market.ticker,
        event_ticker=market.event_ticker,
        title=market.title,
        subtitle=market.subtitle,
        status=market.status,
        close_time=to_eastern_iso(market.close_time),
        close_time_display=eastern_display(market.close_time),
        best_yes_bid=_decimal_float(market.best_yes_bid),
        implied_yes_ask=_decimal_float(market.implied_yes_ask),
        best_no_bid=_decimal_float(market.best_no_bid),
        implied_no_ask=_decimal_float(market.implied_no_ask),
        mapping=mapping_summary,
    )


def _candidate_summary(candidate: ModelCandidate, game: MlbGame | None, market: KalshiMarket | None) -> CandidateSummary:
    game_label = f"{game.away_team} @ {game.home_team}" if game else None
    return CandidateSummary(
        evaluated_at=to_eastern_iso(candidate.evaluated_at),
        evaluated_at_display=eastern_display(candidate.evaluated_at),
        game=game_label,
        market_ticker=market.ticker if market else None,
        market_type=candidate.market_type,
        time_bucket=candidate.time_bucket,
        time_to_start_minutes=candidate.time_to_start_minutes,
        model_probability=_decimal_float(_prefer_not_none(candidate.model_probability, candidate.probability)),
        probability_raw=_decimal_float(candidate.probability_raw),
        probability_calibrated=_decimal_float(candidate.probability_calibrated),
        executable_price=_decimal_float(_prefer_not_none(candidate.executable_price, candidate.market_price)),
        net_expected_value=_decimal_float(candidate.net_expected_value),
        data_quality=_decimal_float(candidate.data_quality),
        calibration_status=candidate.calibration_status,
        training_eligible=candidate.training_eligible,
        decision=candidate.decision,
    )


@app.get("/v1/games/today", response_model=ListResponse)
def games_today() -> ListResponse:
    ready = bool(database_status()["ready"])
    if not ready:
        return ListResponse(items=[], count=0, database_ready=False)
    with _db_session_or_503() as session:
        items = [_game_summary(game).model_dump() for game in list_today_games(session)]
        return ListResponse(items=items, count=len(items), database_ready=True)


@app.get("/v1/markets/today", response_model=ListResponse)
def markets_today() -> ListResponse:
    ready = bool(database_status()["ready"])
    if not ready:
        return ListResponse(items=[], count=0, database_ready=False)
    with _db_session_or_503() as session:
        items = [_market_summary(market, mapping).model_dump() for market, mapping in list_today_markets(session)]
        return ListResponse(items=items, count=len(items), database_ready=True)


@app.get("/v1/candidates/today", response_model=ListResponse)
def candidates_today() -> ListResponse:
    ready = bool(database_status()["ready"])
    if not ready:
        return ListResponse(items=[], count=0, database_ready=False)
    with _db_session_or_503() as session:
        items = [
            _candidate_summary(candidate, game, market).model_dump()
            for candidate, game, market in list_today_candidates(session)
        ]
        return ListResponse(items=items, count=len(items), database_ready=True)


@app.post("/v1/sync/mlb-schedule", response_model=RunResponse)
def run_mlb_schedule_sync(
    target_date: date | None = Query(default=None),
    _: None = Depends(require_internal_api_key),
) -> RunResponse:
    with _db_session_or_503() as session:
        count = sync_schedule(session, target_date)
    return RunResponse(ok=True, action="mlb_schedule_sync", result={"games": count})


@app.post("/v1/sync/kalshi-markets", response_model=RunResponse)
def run_kalshi_market_sync(_: None = Depends(require_internal_api_key)) -> RunResponse:
    if not settings.market_discovery_enabled:
        return RunResponse(ok=True, action="kalshi_market_sync", result={"skipped": True})
    with _db_session_or_503() as session:
        try:
            result = sync_kalshi_markets(session)
        except Exception as exc:
            return RunResponse(
                ok=False,
                action="kalshi_market_sync",
                result={"error": {"message": str(exc), "type": exc.__class__.__name__}},
            )
    errors = result.get("errors")
    ok = not errors or int(result.get("markets_upserted") or 0) > 0
    return RunResponse(ok=ok, action="kalshi_market_sync", result=result)


@app.get("/v1/kalshi/resolve-preview", response_model=RunResponse)
def kalshi_resolve_preview(
    target_date: date = Query(..., alias="date"),
    _: None = Depends(require_internal_api_key),
) -> RunResponse:
    with _db_session_or_503() as session:
        try:
            result = resolve_preview_for_date(session, target_date)
        except Exception as exc:
            return RunResponse(
                ok=False,
                action="kalshi_resolve_preview",
                result={"error": {"message": str(exc), "type": exc.__class__.__name__}},
            )
    return RunResponse(ok=True, action="kalshi_resolve_preview", result=result)


@app.post("/v1/run/paper-candidate-engine", response_model=RunResponse)
def run_paper_candidate_engine(_: None = Depends(require_internal_api_key)) -> RunResponse:
    with _db_session_or_503() as session:
        result = generate_candidates(session)
    return RunResponse(ok=True, action="paper_candidate_engine", result=result)


@app.post("/v1/sync/mlb-results", response_model=RunResponse)
def run_mlb_results_sync(
    target_date: date | None = Query(default=None),
    _: None = Depends(require_internal_api_key),
) -> RunResponse:
    with _db_session_or_503() as session:
        result = sync_results(session, target_date)
    return RunResponse(ok=True, action="mlb_results_sync", result=result)


@app.post("/v1/run/paper-settlement-sync", response_model=RunResponse)
def run_paper_settlement_sync(
    target_date: date | None = Query(default=None),
    _: None = Depends(require_internal_api_key),
) -> RunResponse:
    with _db_session_or_503() as session:
        result = settle_paper_trades(session, target_date)
    return RunResponse(ok=True, action="paper_settlement_sync", result=result)


@app.post("/v1/run/balance-snapshot", response_model=RunResponse)
def run_balance_snapshot(_: None = Depends(require_internal_api_key)) -> RunResponse:
    with _db_session_or_503() as session:
        snapshot = create_balance_snapshot(session, source="manual_endpoint")
        session.commit()
        result = {
            "snapshot_id": snapshot.id,
            "cash_balance": float(snapshot.cash_balance),
            "portfolio_value": float(snapshot.portfolio_value),
            "captured_at": snapshot.captured_at.isoformat(),
        }
    return RunResponse(ok=True, action="balance_snapshot", result=result)


@app.post("/v1/run/open-position-price-refresh", response_model=RunResponse)
def run_open_position_price_refresh(_: None = Depends(require_internal_api_key)) -> RunResponse:
    with _db_session_or_503() as session:
        result = refresh_open_position_prices(session)
    return RunResponse(ok=True, action="open_position_price_refresh", result=result)


@app.post("/v1/run/model-governance", response_model=RunResponse)
def run_model_governance_endpoint(_: None = Depends(require_internal_api_key)) -> RunResponse:
    with _db_session_or_503() as session:
        result = run_model_governance(session)
    return RunResponse(ok=True, action="model_governance", result=result)


@app.get("/v1/model/governance/status", response_model=RunResponse)
def model_governance_status(_: None = Depends(require_internal_api_key)) -> RunResponse:
    with _db_session_or_503() as session:
        result = governance_status(session)
    return RunResponse(ok=True, action="model_governance_status", result=result)


@app.get("/v1/model/features/coverage", response_model=RunResponse)
def model_feature_coverage(
    target_date: date | None = Query(default=None, alias="date"),
    _: None = Depends(require_internal_api_key),
) -> RunResponse:
    with _db_session_or_503() as session:
        result = feature_coverage(session, target_date)
    return RunResponse(ok=True, action="model_feature_coverage", result=result)


@app.get("/v1/model/predictions/today", response_model=RunResponse)
def model_predictions_today(_: None = Depends(require_internal_api_key)) -> RunResponse:
    with _db_session_or_503() as session:
        rows = list(
            session.execute(
                select(ModelPredictionOutput, ModelCandidate, KalshiMarket)
                .join(ModelPredictionRun, ModelPredictionOutput.prediction_run_id == ModelPredictionRun.id)
                .outerjoin(ModelCandidate, ModelPredictionOutput.candidate_id == ModelCandidate.id)
                .outerjoin(KalshiMarket, ModelCandidate.kalshi_market_id == KalshiMarket.id)
                .where(ModelPredictionRun.target_date == today_eastern())
                .order_by(ModelPredictionOutput.id.desc())
                .limit(500)
            )
        )
        items = [
            {
                "candidate_id": candidate.id if candidate else None,
                "market_ticker": market.ticker if market else None,
                "market_family": output.market_family,
                "probability_raw": _decimal_float(output.probability_raw),
                "probability_calibrated": _decimal_float(output.probability_calibrated),
                "fair_value": _decimal_float(output.fair_value),
                "data_quality": _decimal_float(output.data_quality),
                "calibration_status": output.calibration_status,
                "trade_rank": output.trade_rank,
                "decision_reason": output.decision_reason,
            }
            for output, candidate, market in rows
        ]
    return RunResponse(ok=True, action="model_predictions_today", result={"items": items, "count": len(items)})


@app.post("/v1/sync/mlb-features", response_model=RunResponse)
def run_mlb_feature_sync(
    target_date: date | None = Query(default=None),
    _: None = Depends(require_internal_api_key),
) -> RunResponse:
    with _db_session_or_503() as session:
        result = sync_mlb_features(session, target_date)
    return RunResponse(ok=True, action="mlb_feature_sync", result=result)


@app.post("/v1/run/model-feature-snapshot-backfill", response_model=RunResponse)
def run_model_feature_snapshot_backfill(
    target_date: date | None = Query(default=None),
    _: None = Depends(require_internal_api_key),
) -> RunResponse:
    with _db_session_or_503() as session:
        result = sync_mlb_features(session, target_date)
    return RunResponse(ok=True, action="model_feature_snapshot_backfill", result=result)


@app.post("/v1/run/training-eligibility-repair", response_model=RunResponse)
def run_training_eligibility_repair(_: None = Depends(require_internal_api_key)) -> RunResponse:
    with _db_session_or_503() as session:
        result = repair_training_eligibility(session)
    return RunResponse(ok=True, action="training_eligibility_repair", result=result)


@app.post("/v1/run/market-family-discovery", response_model=RunResponse)
def run_market_family_discovery_endpoint(
    target_date: date | None = Query(default=None),
    _: None = Depends(require_internal_api_key),
) -> RunResponse:
    with _db_session_or_503() as session:
        try:
            result = run_market_family_discovery(session, target_date)
        except Exception as exc:
            return RunResponse(
                ok=False,
                action="market_family_discovery",
                result={"status": "failed", "error": {"message": str(exc), "type": exc.__class__.__name__}},
            )
    return RunResponse(ok=result.get("status") != "failed", action="market_family_discovery", result=result)


@app.post("/v1/sync/market-family-mappings", response_model=RunResponse)
def run_market_family_mapping_sync(
    target_date: date | None = Query(default=None),
    _: None = Depends(require_internal_api_key),
) -> RunResponse:
    with _db_session_or_503() as session:
        result = sync_market_family_mappings(session, target_date)
    return RunResponse(ok=result.get("status") != "failed", action="market_family_mapping_sync", result=result)


@app.get("/v1/market-families/mappings", response_model=RunResponse)
def market_family_mapping_report(
    target_date: date | None = Query(default=None, alias="date"),
    _: None = Depends(require_internal_api_key),
) -> RunResponse:
    with _db_session_or_503() as session:
        result = latest_market_family_mapping_report(session, target_date)
    return RunResponse(ok=True, action="market_family_mapping_report", result=result)


@app.get("/v1/market-families/discovery", response_model=RunResponse)
def market_family_discovery_report(
    target_date: date | None = Query(default=None, alias="date"),
    _: None = Depends(require_internal_api_key),
) -> RunResponse:
    with _db_session_or_503() as session:
        result = latest_market_family_discovery(session, target_date)
    return RunResponse(ok=True, action="market_family_discovery_report", result=result)


@app.get("/v1/market-families/discovery-preview", response_model=RunResponse)
def market_family_discovery_preview_endpoint(
    target_date: date | None = Query(default=None, alias="date"),
    _: None = Depends(require_internal_api_key),
) -> RunResponse:
    with _db_session_or_503() as session:
        result = market_family_discovery_preview(session, target_date)
    return RunResponse(ok=True, action="market_family_discovery_preview", result=result)
