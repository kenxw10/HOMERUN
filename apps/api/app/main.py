from datetime import UTC, date, datetime
from decimal import Decimal
import logging
import os
import time

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.orm import load_only

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
    PaperEpochResetRequest,
    RunResponse,
    SystemStatus,
)
from app.security import require_internal_api_key
from app.services.candidates import generate_candidates
from app.services.dashboard import (
    compact_source_status_payload,
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
from app.services.features import (
    feature_coverage,
    feature_detail,
    source_status_report,
    starter_status_report,
    sync_mlb_pregame_context,
    sync_mlb_starters,
    sync_mlb_bullpen_features,
    sync_mlb_features,
    sync_mlb_lineups,
    sync_mlb_pitcher_features,
    sync_mlb_team_features,
    sync_travel_schedule_features,
    sync_weather_features,
)
from app.services.modeling import (
    active_parameter_payload,
    governance_status,
    latest_training_summary,
    repair_training_eligibility,
    run_model_governance,
)
from app.services.job_runs import resolve_job_target_date, run_job
from app.services.market_sync import resolve_preview_for_date, sync_kalshi_markets
from app.services.mlb import sync_results, sync_schedule
from app.services.position_refresh import refresh_open_position_prices
from app.services.portfolio import create_balance_snapshot
from app.services.paper_epoch import get_or_create_active_paper_epoch, reset_paper_trading_epoch
from app.services.settlement import settle_paper_trades
from app.services.ws_market_data import ws_status_payload
from app.time_utils import eastern_display, today_eastern, to_eastern_iso

settings = get_settings()
logger = logging.getLogger(__name__)

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


def _process_rss_bytes() -> int | None:
    try:
        with open("/proc/self/statm", encoding="utf-8") as handle:
            pages = int(handle.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE")
    except Exception:
        return None


def _payload_size_bytes(value: object) -> int | None:
    try:
        dump_json = getattr(value, "model_dump_json", None)
        if callable(dump_json):
            return len(dump_json().encode("utf-8"))
    except Exception:
        return None
    return None


def _log_endpoint_metrics(
    endpoint: str,
    started_at: float,
    result: object | None,
    *,
    flags: dict[str, object] | None = None,
    rss_before: int | None = None,
) -> None:
    try:
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        rss_after = _process_rss_bytes()
        logger.info(
            "endpoint_metrics endpoint=%s duration_ms=%s response_size_bytes=%s rss_before_bytes=%s rss_after_bytes=%s flags=%s",
            endpoint,
            duration_ms,
            _payload_size_bytes(result),
            rss_before,
            rss_after,
            flags or {},
        )
    except Exception:
        return


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
def dashboard_summary(
    closed_date: date | None = Query(default=None),
    epoch_key: str | None = Query(default=None),
    include_archived: bool = Query(default=False),
    include_pre_observation: bool = Query(default=False),
    include_diagnostics: bool = Query(default=False),
    include_job_results: bool = Query(default=False),
    include_source_details: bool = Query(default=False),
    include_governance_details: bool = Query(default=False),
    include_spread_audit_details: bool = Query(default=False),
    include_candidate_diagnostics: bool = Query(default=False),
    x_api_key: str | None = Header(default=None),
) -> DashboardSummary:
    started_at = time.perf_counter()
    rss_before = _process_rss_bytes()
    flags = {
        "include_archived": include_archived,
        "include_pre_observation": include_pre_observation,
        "include_diagnostics": include_diagnostics,
        "include_job_results": include_job_results,
        "include_source_details": include_source_details,
        "include_governance_details": include_governance_details,
        "include_spread_audit_details": include_spread_audit_details,
        "include_candidate_diagnostics": include_candidate_diagnostics,
    }
    if any(
        (
            include_diagnostics,
            include_job_results,
            include_source_details,
            include_governance_details,
            include_spread_audit_details,
            include_candidate_diagnostics,
        )
    ):
        require_internal_api_key(x_api_key=x_api_key)
    if not database_status()["ready"]:
        result = empty_dashboard_summary(closed_date)
        _log_endpoint_metrics("/v1/dashboard/summary", started_at, result, flags=flags, rss_before=rss_before)
        return result

    try:
        session_factory = get_session_factory()
        with session_factory() as session:
            result = dashboard_summary_from_db(
                session,
                closed_date,
                epoch_key=epoch_key,
                include_archived=include_archived,
                include_pre_observation=include_pre_observation,
                include_diagnostics=include_diagnostics,
                include_job_results=include_job_results,
                include_source_details=include_source_details,
                include_governance_details=include_governance_details,
                include_spread_audit_details=include_spread_audit_details,
                include_candidate_diagnostics=include_candidate_diagnostics,
            )
            _log_endpoint_metrics("/v1/dashboard/summary", started_at, result, flags=flags, rss_before=rss_before)
            return result
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception:
        result = empty_dashboard_summary(closed_date)
        _log_endpoint_metrics("/v1/dashboard/summary", started_at, result, flags=flags, rss_before=rss_before)
        return result


@app.get("/v1/system/status", response_model=SystemStatus)
def system_status() -> SystemStatus:
    db_status = database_status()
    credentials_state = "set_redacted" if settings.kalshi_credentials_configured else "not_set"
    source_status: dict[str, object] = {
        "feature_sync_enable_network_sources": settings.feature_sync_enable_network_sources,
        "public_sources_enabled": settings.feature_sync_enable_network_sources,
        "mlb_stats_base_url": settings.mlb_stats_base_url,
        "open_meteo_base_url": settings.open_meteo_base_url,
    }
    if db_status["ready"]:
        try:
            session_factory = get_session_factory()
            with session_factory() as session:
                full_source_status = source_status_report(session)
                compact_source_status = compact_source_status_payload(full_source_status)
                config_fields = {
                    key: full_source_status[key]
                    for key in (
                        "feature_sync_enable_network_sources",
                        "public_sources_enabled",
                        "mlb_stats_base_url",
                        "open_meteo_base_url",
                        "optional_injury_provider_configured",
                        "optional_lineup_provider_configured",
                        "optional_weather_provider_configured",
                    )
                    if key in full_source_status
                }
                source_status = {**compact_source_status, **config_fields}
        except Exception:
            source_status["last_error"] = "source status unavailable"

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
            feature_sync_enable_network_sources=settings.feature_sync_enable_network_sources,
            public_sources_enabled=settings.feature_sync_enable_network_sources,
            source_status=source_status,
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
    rationale = candidate.scoring_rationale if isinstance(candidate.scoring_rationale, dict) else {}
    return CandidateSummary(
        evaluated_at=to_eastern_iso(candidate.evaluated_at),
        evaluated_at_display=eastern_display(candidate.evaluated_at),
        game=game_label,
        market_ticker=market.ticker if market else None,
        market_type=candidate.market_type,
        contract_side=candidate.contract_side,
        contract_display=candidate.contract_display,
        normalized_equivalent_display=rationale.get("normalized_equivalent_display"),
        display_title=rationale.get("display_title"),
        display_subtitle=rationale.get("display_subtitle"),
        raw_ticker_display=rationale.get("raw_ticker_display"),
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
        probability_adapter_key=candidate.probability_adapter_key,
        probability_adapter_version=candidate.probability_adapter_version,
        probability_adapter_policy_version=candidate.probability_adapter_policy_version,
        probability_adapter_family=candidate.probability_adapter_family,
        probability_adapter_scope=candidate.probability_adapter_scope,
        probability_adapter_calibration_hook=candidate.probability_adapter_calibration_hook,
        probability_adapter_calibration_version=candidate.probability_adapter_calibration_version,
        probability_adapter_feature_policy_version=candidate.probability_adapter_feature_policy_version,
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
def run_paper_candidate_engine(
    target_date: date | None = Query(default=None),
    _: None = Depends(require_internal_api_key),
) -> RunResponse:
    with _db_session_or_503() as session:
        result = generate_candidates(session, target_date)
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
    include_archived: bool = Query(default=False),
    _: None = Depends(require_internal_api_key),
) -> RunResponse:
    with _db_session_or_503() as session:
        result = settle_paper_trades(session, target_date, include_archived=include_archived)
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


@app.post("/v1/admin/paper-trading/reset-epoch", response_model=RunResponse)
def reset_paper_epoch_endpoint(
    request: PaperEpochResetRequest,
    _: None = Depends(require_internal_api_key),
) -> RunResponse:
    with _db_session_or_503() as session:
        try:
            result = reset_paper_trading_epoch(
                session,
                archive_current_as=request.archive_current_as,
                new_epoch=request.new_epoch,
                starting_balance=Decimal(str(request.starting_balance)),
                archive_open_positions=request.archive_open_positions,
                reset_dashboard_metrics=request.reset_dashboard_metrics,
                confirmation=request.confirmation,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return RunResponse(ok=True, action="paper_trading_reset_epoch", result=result)


@app.post("/v1/run/open-position-price-refresh", response_model=RunResponse)
def run_open_position_price_refresh(
    include_archived: bool = Query(default=False),
    _: None = Depends(require_internal_api_key),
) -> RunResponse:
    with _db_session_or_503() as session:
        result = refresh_open_position_prices(session, include_archived=include_archived)
    return RunResponse(ok=True, action="open_position_price_refresh", result=result)


@app.get("/v1/ws/status", response_model=RunResponse)
def websocket_status(_: None = Depends(require_internal_api_key)) -> RunResponse:
    with _db_session_or_503() as session:
        result = ws_status_payload(session)
        session.commit()
    return RunResponse(ok=True, action="websocket_status", result=result)


def _run_named_job(
    job_name: str,
    target_date: str | date | None,
    triggered_by: str = "api",
    *,
    min_time_to_start_minutes: int | None = None,
    max_time_to_start_minutes: int | None = None,
    sweep_label: str | None = None,
    dry_run_candidates_only: bool = False,
) -> RunResponse:
    resolved_target_date = _resolve_api_target_date(target_date)
    with _db_session_or_503() as session:
        try:
            result = run_job(
                session,
                job_name=job_name,
                target_date=resolved_target_date,
                triggered_by=triggered_by,
                min_time_to_start_minutes=min_time_to_start_minutes,
                max_time_to_start_minutes=max_time_to_start_minutes,
                sweep_label=sweep_label,
                dry_run_candidates_only=dry_run_candidates_only,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return RunResponse(ok=result.get("status") not in {"failed", "failed_stale"}, action=f"job_{job_name}", result=result)


def _resolve_api_target_date(target_date: str | date | None, *, parameter_name: str = "target_date") -> date | None:
    try:
        return resolve_job_target_date(target_date)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid {parameter_name}. Use YYYY-MM-DD, today_et, or yesterday_et.",
        ) from exc


@app.post("/v1/jobs/run/daily-setup", response_model=RunResponse)
def run_daily_setup_job(
    target_date: str | None = Query(default=None),
    _: None = Depends(require_internal_api_key),
) -> RunResponse:
    return _run_named_job("daily-setup", target_date)


@app.post("/v1/jobs/run/candidate-sweep", response_model=RunResponse)
def run_candidate_sweep_job(
    target_date: str | None = Query(default=None),
    min_time_to_start_minutes: int | None = Query(default=None),
    max_time_to_start_minutes: int | None = Query(default=None),
    sweep_label: str | None = Query(default=None),
    dry_run_candidates_only: bool = Query(default=False),
    _: None = Depends(require_internal_api_key),
) -> RunResponse:
    return _run_named_job(
        "candidate-sweep",
        target_date,
        min_time_to_start_minutes=min_time_to_start_minutes,
        max_time_to_start_minutes=max_time_to_start_minutes,
        sweep_label=sweep_label,
        dry_run_candidates_only=dry_run_candidates_only,
    )


@app.post("/v1/jobs/run/price-refresh", response_model=RunResponse)
def run_price_refresh_job(
    target_date: str | None = Query(default=None),
    _: None = Depends(require_internal_api_key),
) -> RunResponse:
    return _run_named_job("price-refresh", target_date)


@app.post("/v1/jobs/run/settlement", response_model=RunResponse)
def run_settlement_job(
    target_date: str | None = Query(default=None),
    _: None = Depends(require_internal_api_key),
) -> RunResponse:
    return _run_named_job("settlement", target_date)


@app.post("/v1/jobs/run/governance", response_model=RunResponse)
def run_governance_job(_: None = Depends(require_internal_api_key)) -> RunResponse:
    return _run_named_job("governance", None)


@app.post("/v1/jobs/run/full-paper-cycle", response_model=RunResponse)
def run_full_paper_cycle_job(
    target_date: str | None = Query(default=None),
    _: None = Depends(require_internal_api_key),
) -> RunResponse:
    return _run_named_job("full-paper-cycle", target_date)


@app.post("/v1/jobs/run/spread-audit", response_model=RunResponse)
def run_spread_audit_job(
    target_date: str | None = Query(default=None),
    min_time_to_start_minutes: int | None = Query(default=45),
    max_time_to_start_minutes: int | None = Query(default=180),
    _: None = Depends(require_internal_api_key),
) -> RunResponse:
    return _run_named_job(
        "spread-audit",
        target_date,
        min_time_to_start_minutes=min_time_to_start_minutes,
        max_time_to_start_minutes=max_time_to_start_minutes,
    )


@app.post("/v1/run/model-governance", response_model=RunResponse)
def run_model_governance_endpoint(_: None = Depends(require_internal_api_key)) -> RunResponse:
    with _db_session_or_503() as session:
        active_epoch = get_or_create_active_paper_epoch(session)
        result = run_model_governance(session, paper_trading_epoch_id=active_epoch.id)
    return RunResponse(ok=True, action="model_governance", result=result)


@app.get("/v1/model/governance/status", response_model=RunResponse)
def model_governance_status(
    include_details: bool = Query(default=False),
    _: None = Depends(require_internal_api_key),
) -> RunResponse:
    started_at = time.perf_counter()
    rss_before = _process_rss_bytes()
    with _db_session_or_503() as session:
        result = governance_status(session, include_details=include_details)
    response = RunResponse(ok=True, action="model_governance_status", result=result)
    _log_endpoint_metrics(
        "/v1/model/governance/status",
        started_at,
        response,
        flags={"include_details": include_details},
        rss_before=rss_before,
    )
    return response


@app.get("/v1/model/features/coverage", response_model=RunResponse)
def model_feature_coverage(
    target_date: date | None = Query(default=None, alias="date"),
    _: None = Depends(require_internal_api_key),
) -> RunResponse:
    with _db_session_or_503() as session:
        result = feature_coverage(session, target_date)
    return RunResponse(ok=True, action="model_feature_coverage", result=result)


@app.get("/v1/model/features/detail", response_model=RunResponse)
def model_feature_detail(
    target_date: date | None = Query(default=None, alias="date"),
    _: None = Depends(require_internal_api_key),
) -> RunResponse:
    with _db_session_or_503() as session:
        result = feature_detail(session, target_date)
    return RunResponse(ok=True, action="model_feature_detail", result=result)


@app.get("/v1/model/sources/status", response_model=RunResponse)
def model_sources_status(_: None = Depends(require_internal_api_key)) -> RunResponse:
    started_at = time.perf_counter()
    rss_before = _process_rss_bytes()
    with _db_session_or_503() as session:
        result = source_status_report(session)
    response = RunResponse(ok=True, action="model_sources_status", result=result)
    _log_endpoint_metrics("/v1/model/sources/status", started_at, response, rss_before=rss_before)
    return response


@app.get("/v1/model/parameters/active", response_model=RunResponse)
def model_active_parameters(_: None = Depends(require_internal_api_key)) -> RunResponse:
    started_at = time.perf_counter()
    rss_before = _process_rss_bytes()
    with _db_session_or_503() as session:
        result = active_parameter_payload(session)
        session.commit()
    response = RunResponse(ok=True, action="model_active_parameters", result=result)
    _log_endpoint_metrics("/v1/model/parameters/active", started_at, response, rss_before=rss_before)
    return response


@app.get("/v1/model/training/latest", response_model=RunResponse)
def model_training_latest(_: None = Depends(require_internal_api_key)) -> RunResponse:
    started_at = time.perf_counter()
    rss_before = _process_rss_bytes()
    with _db_session_or_503() as session:
        result = latest_training_summary(session)
    response = RunResponse(ok=True, action="model_training_latest", result=result)
    _log_endpoint_metrics("/v1/model/training/latest", started_at, response, rss_before=rss_before)
    return response


def _model_predictions_for_date(target_date: date) -> dict[str, object]:
    with _db_session_or_503() as session:
        active_epoch = get_or_create_active_paper_epoch(session)
        rows = list(
            session.execute(
                select(ModelPredictionOutput, ModelCandidate, KalshiMarket, ModelPredictionRun)
                .join(ModelPredictionRun, ModelPredictionOutput.prediction_run_id == ModelPredictionRun.id)
                .outerjoin(ModelCandidate, ModelPredictionOutput.candidate_id == ModelCandidate.id)
                .outerjoin(KalshiMarket, ModelCandidate.kalshi_market_id == KalshiMarket.id)
                .where(ModelPredictionRun.target_date == target_date)
                .where(ModelPredictionOutput.paper_trading_epoch_id == active_epoch.id)
                .options(
                    load_only(
                        ModelPredictionOutput.id,
                        ModelPredictionOutput.prediction_run_id,
                        ModelPredictionOutput.market_family,
                        ModelPredictionOutput.probability_raw,
                        ModelPredictionOutput.probability_calibrated,
                        ModelPredictionOutput.fair_value,
                        ModelPredictionOutput.executable_price,
                        ModelPredictionOutput.expected_value_gross,
                        ModelPredictionOutput.fee_estimate,
                        ModelPredictionOutput.expected_value_net,
                        ModelPredictionOutput.probability_edge,
                        ModelPredictionOutput.executable_price_source,
                        ModelPredictionOutput.price_status,
                        ModelPredictionOutput.data_quality,
                        ModelPredictionOutput.calibration_status,
                        ModelPredictionOutput.trade_rank,
                        ModelPredictionOutput.decision_reason,
                    ),
                    load_only(
                        ModelCandidate.id,
                        ModelCandidate.kalshi_market_id,
                        ModelCandidate.decision,
                        ModelCandidate.market_type,
                        ModelCandidate.market_family,
                        ModelCandidate.contract_side,
                        ModelCandidate.line_value,
                        ModelCandidate.economic_exposure_label,
                        ModelCandidate.economic_exposure_key,
                        ModelCandidate.economic_exposure_family,
                        ModelCandidate.economic_exposure_scope,
                        ModelCandidate.economic_exposure_direction,
                        ModelCandidate.economic_exposure_team,
                        ModelCandidate.economic_exposure_line,
                        ModelCandidate.contract_mechanics_label,
                        ModelCandidate.concept_cluster_key,
                        ModelCandidate.same_game_concept_cluster_key,
                        ModelCandidate.line_class,
                        ModelCandidate.line_class_reason,
                        ModelCandidate.line_ladder_rank,
                        ModelCandidate.line_ladder_distance_from_central,
                        ModelCandidate.line_ladder_size,
                        ModelCandidate.exposure_taxonomy_version,
                        ModelCandidate.line_classification_policy_version,
                        ModelCandidate.selector_policy_version,
                        ModelCandidate.selector_mode,
                        ModelCandidate.selector_status,
                        ModelCandidate.selector_decision,
                        ModelCandidate.selector_rejection_reason,
                        ModelCandidate.selector_threshold_profile,
                        ModelCandidate.selector_min_net_ev,
                        ModelCandidate.selector_min_prob_edge,
                        ModelCandidate.selector_min_data_quality,
                        ModelCandidate.selector_line_class_policy,
                        ModelCandidate.selector_concept_cluster_key,
                        ModelCandidate.selector_same_game_concept_cluster_key,
                        ModelCandidate.selector_cluster_rank,
                        ModelCandidate.selector_cluster_rank_score,
                        ModelCandidate.selector_selected_from_cluster,
                        ModelCandidate.selector_shadow_only,
                        ModelCandidate.selector_live_like_eligible_before_cluster,
                        ModelCandidate.selector_live_like_eligible_after_cluster,
                        ModelCandidate.probability_adapter_key,
                        ModelCandidate.probability_adapter_version,
                        ModelCandidate.probability_adapter_policy_version,
                        ModelCandidate.probability_adapter_family,
                        ModelCandidate.probability_adapter_scope,
                        ModelCandidate.probability_adapter_rationale,
                        ModelCandidate.probability_adapter_calibration_hook,
                        ModelCandidate.probability_adapter_calibration_version,
                        ModelCandidate.probability_adapter_feature_policy_version,
                    ),
                    load_only(KalshiMarket.id, KalshiMarket.ticker),
                    load_only(ModelPredictionRun.id, ModelPredictionRun.target_date),
                )
                .order_by(ModelPredictionOutput.id.desc())
                .limit(500)
            )
        )
        items = [
            {
                "prediction_run_id": run.id,
                "prediction_run_target_date": run.target_date.isoformat() if run.target_date else None,
                "candidate_id": candidate.id if candidate else None,
                "market_ticker": market.ticker if market else None,
                "market_family": output.market_family,
                "market_type": candidate.market_type if candidate else None,
                "contract_side": candidate.contract_side if candidate else None,
                "decision": candidate.decision if candidate else output.decision_reason,
                "line_value": _decimal_float(candidate.line_value) if candidate else None,
                **_candidate_prediction_taxonomy(candidate),
                **_candidate_prediction_selector(candidate),
                **_candidate_prediction_probability_adapter(candidate),
                "probability_raw": _decimal_float(output.probability_raw),
                "probability_calibrated": _decimal_float(output.probability_calibrated),
                "fair_value": _decimal_float(output.fair_value),
                "executable_price": _decimal_float(output.executable_price),
                "expected_value_gross": _decimal_float(output.expected_value_gross),
                "fee_estimate": _decimal_float(output.fee_estimate),
                "expected_value_net": _decimal_float(output.expected_value_net),
                "probability_edge": _decimal_float(output.probability_edge),
                "executable_price_source": output.executable_price_source,
                "price_status": output.price_status,
                "data_quality": _decimal_float(output.data_quality),
                "calibration_status": output.calibration_status,
                "trade_rank": output.trade_rank,
                "decision_reason": output.decision_reason,
            }
            for output, candidate, market, run in rows
        ]
    return {"date": target_date.isoformat(), "items": items, "count": len(items)}


def _candidate_prediction_taxonomy(candidate: ModelCandidate | None) -> dict[str, object]:
    if candidate is None:
        return {
            "economic_exposure_label": None,
            "economic_exposure_key": None,
            "economic_exposure_family": None,
            "economic_exposure_scope": None,
            "economic_exposure_direction": None,
            "economic_exposure_team": None,
            "economic_exposure_line": None,
            "contract_mechanics_label": None,
            "concept_cluster_key": None,
            "same_game_concept_cluster_key": None,
            "line_class": None,
            "line_class_reason": None,
            "line_ladder_rank": None,
            "line_ladder_distance_from_central": None,
            "line_ladder_size": None,
            "exposure_taxonomy_version": None,
            "line_classification_policy_version": None,
        }
    return {
        "economic_exposure_label": candidate.economic_exposure_label,
        "economic_exposure_key": candidate.economic_exposure_key,
        "economic_exposure_family": candidate.economic_exposure_family,
        "economic_exposure_scope": candidate.economic_exposure_scope,
        "economic_exposure_direction": candidate.economic_exposure_direction,
        "economic_exposure_team": candidate.economic_exposure_team,
        "economic_exposure_line": _decimal_float(candidate.economic_exposure_line),
        "contract_mechanics_label": candidate.contract_mechanics_label,
        "concept_cluster_key": candidate.concept_cluster_key,
        "same_game_concept_cluster_key": candidate.same_game_concept_cluster_key,
        "line_class": candidate.line_class,
        "line_class_reason": candidate.line_class_reason,
        "line_ladder_rank": candidate.line_ladder_rank,
        "line_ladder_distance_from_central": candidate.line_ladder_distance_from_central,
        "line_ladder_size": candidate.line_ladder_size,
        "exposure_taxonomy_version": candidate.exposure_taxonomy_version,
        "line_classification_policy_version": candidate.line_classification_policy_version,
    }


def _candidate_prediction_selector(candidate: ModelCandidate | None) -> dict[str, object]:
    if candidate is None:
        return {
            "selector_policy_version": None,
            "selector_mode": None,
            "selector_status": None,
            "selector_decision": None,
            "selector_rejection_reason": None,
            "selector_threshold_profile": None,
            "selector_min_net_ev": None,
            "selector_min_prob_edge": None,
            "selector_min_data_quality": None,
            "selector_line_class_policy": None,
            "selector_concept_cluster_key": None,
            "selector_same_game_concept_cluster_key": None,
            "selector_cluster_rank": None,
            "selector_cluster_rank_score": None,
            "selector_selected_from_cluster": None,
            "selector_shadow_only": None,
            "selector_live_like_eligible_before_cluster": None,
            "selector_live_like_eligible_after_cluster": None,
        }
    return {
        "selector_policy_version": candidate.selector_policy_version,
        "selector_mode": candidate.selector_mode,
        "selector_status": candidate.selector_status,
        "selector_decision": candidate.selector_decision,
        "selector_rejection_reason": candidate.selector_rejection_reason,
        "selector_threshold_profile": candidate.selector_threshold_profile,
        "selector_min_net_ev": _decimal_float(candidate.selector_min_net_ev),
        "selector_min_prob_edge": _decimal_float(candidate.selector_min_prob_edge),
        "selector_min_data_quality": _decimal_float(candidate.selector_min_data_quality),
        "selector_line_class_policy": candidate.selector_line_class_policy,
        "selector_concept_cluster_key": candidate.selector_concept_cluster_key,
        "selector_same_game_concept_cluster_key": candidate.selector_same_game_concept_cluster_key,
        "selector_cluster_rank": candidate.selector_cluster_rank,
        "selector_cluster_rank_score": _decimal_float(candidate.selector_cluster_rank_score),
        "selector_selected_from_cluster": candidate.selector_selected_from_cluster,
        "selector_shadow_only": candidate.selector_shadow_only,
        "selector_live_like_eligible_before_cluster": candidate.selector_live_like_eligible_before_cluster,
        "selector_live_like_eligible_after_cluster": candidate.selector_live_like_eligible_after_cluster,
    }


def _candidate_prediction_probability_adapter(candidate: ModelCandidate | None) -> dict[str, object]:
    if candidate is None:
        return {
            "probability_adapter_key": None,
            "probability_adapter_version": None,
            "probability_adapter_policy_version": None,
            "probability_adapter_family": None,
            "probability_adapter_scope": None,
            "probability_adapter_rationale": None,
            "probability_adapter_calibration_hook": None,
            "probability_adapter_calibration_version": None,
            "probability_adapter_feature_policy_version": None,
        }
    return {
        "probability_adapter_key": candidate.probability_adapter_key,
        "probability_adapter_version": candidate.probability_adapter_version,
        "probability_adapter_policy_version": candidate.probability_adapter_policy_version,
        "probability_adapter_family": candidate.probability_adapter_family,
        "probability_adapter_scope": candidate.probability_adapter_scope,
        "probability_adapter_rationale": candidate.probability_adapter_rationale,
        "probability_adapter_calibration_hook": candidate.probability_adapter_calibration_hook,
        "probability_adapter_calibration_version": candidate.probability_adapter_calibration_version,
        "probability_adapter_feature_policy_version": candidate.probability_adapter_feature_policy_version,
    }


@app.get("/v1/model/predictions", response_model=RunResponse)
def model_predictions_by_date(
    target_date: date = Query(..., alias="date"),
    _: None = Depends(require_internal_api_key),
) -> RunResponse:
    return RunResponse(ok=True, action="model_predictions", result=_model_predictions_for_date(target_date))


@app.get("/v1/model/predictions/today", response_model=RunResponse)
def model_predictions_today(_: None = Depends(require_internal_api_key)) -> RunResponse:
    return RunResponse(ok=True, action="model_predictions_today", result=_model_predictions_for_date(today_eastern()))


@app.post("/v1/sync/mlb-features", response_model=RunResponse)
def run_mlb_feature_sync(
    target_date: date | None = Query(default=None),
    include_modules: str | None = Query(default=None),
    refresh_schedule: bool | None = Query(default=None),
    _: None = Depends(require_internal_api_key),
) -> RunResponse:
    with _db_session_or_503() as session:
        modules = None if not include_modules or include_modules == "all" else set(include_modules.split(","))
        result = sync_mlb_features(session, target_date, modules, refresh_schedule)
    return RunResponse(ok=True, action="mlb_feature_sync", result=result)


@app.post("/v1/sync/mlb-starters", response_model=RunResponse)
def run_mlb_starter_sync(
    target_date: str | None = Query(default=None),
    _: None = Depends(require_internal_api_key),
) -> RunResponse:
    resolved_target_date = _resolve_api_target_date(target_date)
    with _db_session_or_503() as session:
        result = sync_mlb_starters(session, resolved_target_date)
    return RunResponse(ok=result.get("validation_status") != "failed", action="mlb_starter_sync", result=result)


@app.post("/v1/sync/mlb-pregame-context", response_model=RunResponse)
def run_mlb_pregame_context_sync(
    target_date: str | None = Query(default=None),
    _: None = Depends(require_internal_api_key),
) -> RunResponse:
    resolved_target_date = _resolve_api_target_date(target_date)
    with _db_session_or_503() as session:
        result = sync_mlb_pregame_context(session, resolved_target_date)
    return RunResponse(ok=result.get("validation_status") != "failed", action="mlb_pregame_context_sync", result=result)


@app.get("/v1/model/starter-status", response_model=RunResponse)
def model_starter_status(
    target_date: str | None = Query(default=None, alias="date"),
    _: None = Depends(require_internal_api_key),
) -> RunResponse:
    resolved_target_date = _resolve_api_target_date(target_date, parameter_name="date")
    with _db_session_or_503() as session:
        result = starter_status_report(session, resolved_target_date)
    return RunResponse(ok=True, action="starter_status", result=result)


@app.post("/v1/sync/mlb-team-features", response_model=RunResponse)
def run_mlb_team_feature_sync(
    target_date: date | None = Query(default=None),
    refresh_schedule: bool | None = Query(default=None),
    _: None = Depends(require_internal_api_key),
) -> RunResponse:
    with _db_session_or_503() as session:
        result = sync_mlb_team_features(session, target_date, refresh_schedule)
    return RunResponse(ok=True, action="mlb_team_feature_sync", result=result)


@app.post("/v1/sync/mlb-pitcher-features", response_model=RunResponse)
def run_mlb_pitcher_feature_sync(
    target_date: date | None = Query(default=None),
    refresh_schedule: bool | None = Query(default=None),
    _: None = Depends(require_internal_api_key),
) -> RunResponse:
    with _db_session_or_503() as session:
        result = sync_mlb_pitcher_features(session, target_date, refresh_schedule)
    return RunResponse(ok=True, action="mlb_pitcher_feature_sync", result=result)


@app.post("/v1/sync/mlb-lineups", response_model=RunResponse)
def run_mlb_lineup_sync(
    target_date: date | None = Query(default=None),
    refresh_schedule: bool | None = Query(default=None),
    _: None = Depends(require_internal_api_key),
) -> RunResponse:
    with _db_session_or_503() as session:
        result = sync_mlb_lineups(session, target_date, refresh_schedule)
    return RunResponse(ok=True, action="mlb_lineup_sync", result=result)


@app.post("/v1/sync/mlb-bullpen-features", response_model=RunResponse)
def run_mlb_bullpen_feature_sync(
    target_date: date | None = Query(default=None),
    refresh_schedule: bool | None = Query(default=None),
    _: None = Depends(require_internal_api_key),
) -> RunResponse:
    with _db_session_or_503() as session:
        result = sync_mlb_bullpen_features(session, target_date, refresh_schedule)
    return RunResponse(ok=True, action="mlb_bullpen_feature_sync", result=result)


@app.post("/v1/sync/weather", response_model=RunResponse)
def run_weather_feature_sync(
    target_date: date | None = Query(default=None),
    refresh_schedule: bool | None = Query(default=None),
    _: None = Depends(require_internal_api_key),
) -> RunResponse:
    with _db_session_or_503() as session:
        result = sync_weather_features(session, target_date, refresh_schedule)
    return RunResponse(ok=True, action="weather_feature_sync", result=result)


@app.post("/v1/sync/travel-schedule", response_model=RunResponse)
def run_travel_schedule_feature_sync(
    target_date: date | None = Query(default=None),
    refresh_schedule: bool | None = Query(default=None),
    _: None = Depends(require_internal_api_key),
) -> RunResponse:
    with _db_session_or_503() as session:
        result = sync_travel_schedule_features(session, target_date, refresh_schedule)
    return RunResponse(ok=True, action="travel_schedule_feature_sync", result=result)


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
    force_refresh: bool = Query(default=False),
    _: None = Depends(require_internal_api_key),
) -> RunResponse:
    with _db_session_or_503() as session:
        try:
            result = run_market_family_discovery(session, target_date, force_refresh=force_refresh)
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
