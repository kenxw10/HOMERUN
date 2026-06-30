from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    BalanceSnapshot,
    JobRun,
    KalshiMarket,
    MarketMapping,
    MlbGame,
    MlbFeatureSnapshot,
    ModelCandidate,
    ModelParameterVersion,
    ModelPredictionRun,
    ModelVersion,
    PaperTrade,
    PaperTradingEpoch,
    Position,
    MarketDataWorkerStatus,
)
from app.schemas import (
    BotMode,
    DashboardSummary,
    ModelStatus,
    PerformanceMetrics,
    PortfolioPoint,
    PositionSummary,
    ActiveEpochSummary,
    JobRunSummary,
    WebSocketStatusSummary,
)
from app.services.contracts import contract_labels, market_type_from_ticker
from app.services.features import FEATURE_VERSION, source_status_report
from app.services.modeling import latest_governance_artifacts
from app.services.portfolio import calculate_paper_portfolio, paper_trade_fee
from app.services.paper_epoch import resolve_epoch_filter
from app.services.ws_market_data import ws_status_running_is_fresh
from app.time_utils import eastern_display, ensure_aware_utc, get_dashboard_zone, to_eastern_iso, today_eastern, utc_now

MAPPING_STATUS_PRIORITY = {"confirmed": 0, "candidate": 1, "needs_review": 2}


def empty_dashboard_summary(closed_date: date | None = None) -> DashboardSummary:
    settings = get_settings()
    selected_closed_date = closed_date or today_eastern()
    return DashboardSummary(
        portfolio_series=[],
        performance=PerformanceMetrics(win_rate=None, roi=None, profit_loss=0.0, record="0-0-0"),
        positions=[],
        closed_positions=[],
        closed_positions_date=selected_closed_date.isoformat(),
        closed_positions_count=0,
        bot=BotMode(
            mode="paper",
            paper_trading=settings.paper_trading,
            live_trading_enabled=settings.live_trading_enabled,
            execution_kill_switch=settings.execution_kill_switch,
            kalshi_env=settings.kalshi_env,
        ),
        model_status=ModelStatus(
            active_model_version=None,
            active_parameter_version=None,
            active_calibration_version=None,
            feature_version=None,
            calibration_status="not_run",
            last_training_run=None,
            last_calibration_run=None,
            candidate_count=0,
            resolved_mature_samples=0,
            training_eligible_count=0,
            last_governance_status="not_run",
            governance_status="not_run",
            trade_policy={},
            trade_caps_used={},
            trade_threshold_policy={},
            data_quality_summary={},
            feature_completeness={},
            source_statuses={},
            critical_module_warnings=[],
            lineup_status="missing",
            starter_status="missing",
            weather_status="missing",
            notes=["No mature model run has been recorded yet."],
        ),
        paper_starting_balance=float(settings.paper_starting_balance),
        performance_by_scope={},
        performance_by_family={},
        decision_breakdown_by_scope={},
        decision_breakdown_by_family={},
        latest_candidate_diagnostics={},
        job_status={},
        websocket_status=WebSocketStatusSummary(
            enabled=settings.websocket_market_data_enabled,
            running=False,
            source="rest_fallback",
        ),
        last_update=to_eastern_iso(utc_now()),
        last_update_display=eastern_display(utc_now()),
    )


def _float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _first_decimal(*values: Decimal | None) -> Decimal:
    for value in values:
        if value is not None:
            return value
    raise ValueError("at least one decimal value is required")


def _game_status_display(game: MlbGame | None) -> str:
    if game is None:
        return "UNKNOWN"
    status = game.status.strip().lower()
    if any(token in status for token in ("final", "completed", "game over")):
        return "FINAL"
    if any(token in status for token in ("cancel", "canceled", "cancelled")):
        return "CANCELED"
    if "postpon" in status:
        return "POSTPONED"
    if any(token in status for token in ("in progress", "live", "warmup", "delayed", "suspended")):
        return "LIVE"
    if status in {"scheduled", "pre-game", "preview"}:
        return "NOT STARTED"
    return "UNKNOWN"


def _position_from_trade(
    trade: PaperTrade,
    game: MlbGame | None = None,
    market: KalshiMarket | None = None,
) -> PositionSummary:
    current = _first_decimal(trade.exit_price, trade.current_price, trade.entry_price)
    cost_basis = (trade.entry_price * trade.quantity) + paper_trade_fee(trade)
    pnl = (
        trade.realized_pnl
        if trade.realized_pnl is not None
        else ((current * trade.quantity) - cost_basis).quantize(Decimal("0.01"))
    )
    pnl_percent = (pnl / cost_basis).quantize(Decimal("0.0001")) if cost_basis else None
    fallback_labels = contract_labels(
        game=game,
        market=market,
        market_ticker=trade.market_ticker,
        market_type=market_type_from_ticker(trade.market_ticker),
        selection_code=trade.selection_code or (market.selection_code if market else None),
        contract_side=trade.contract_side,
    )
    display = trade.contract_display or trade.market_display or fallback_labels.contract_display
    return PositionSummary(
        time_entered=to_eastern_iso(trade.entry_time),
        time_entered_display=eastern_display(trade.entry_time),
        time_closed=to_eastern_iso(trade.exit_time or trade.settled_at),
        time_closed_display=eastern_display(trade.exit_time or trade.settled_at),
        market=display,
        market_ticker=trade.market_ticker,
        market_display=trade.market_display or fallback_labels.market_display,
        selection_display=trade.selection_display or fallback_labels.selection_display,
        matchup_display=trade.matchup_display or fallback_labels.matchup_display,
        contract_display=display,
        normalized_equivalent_display=fallback_labels.normalized_equivalent_display,
        side=trade.contract_side,
        entry_price=float(trade.entry_price),
        exit_price=float(trade.exit_price) if trade.exit_price is not None else None,
        current_price=float(current),
        current_price_updated_at=to_eastern_iso(trade.current_price_updated_at),
        current_price_updated_at_display=eastern_display(trade.current_price_updated_at),
        quantity=trade.quantity,
        profit_loss=float(pnl),
        profit_loss_percent=_float(pnl_percent),
        status=trade.status,
        game_status=_game_status_display(game),
        game_status_display=_game_status_display(game),
        resolution=trade.resolution,
        outcome=trade.outcome,
    )


def _position_from_position(position: Position) -> PositionSummary:
    current = position.current_price if position.current_price is not None else position.entry_price
    pnl = ((current - position.entry_price) * position.quantity).quantize(Decimal("0.01"))
    pnl_percent = ((current - position.entry_price) / position.entry_price).quantize(Decimal("0.0001")) if position.entry_price else None
    fallback_labels = contract_labels(
        game=None,
        market=None,
        market_ticker=position.market_ticker,
        market_type=market_type_from_ticker(position.market_ticker),
        contract_side=position.contract_side,
    )
    return PositionSummary(
        time_entered=to_eastern_iso(position.opened_at),
        time_entered_display=eastern_display(position.opened_at),
        time_closed=to_eastern_iso(position.closed_at),
        time_closed_display=eastern_display(position.closed_at),
        market=fallback_labels.contract_display,
        market_ticker=position.market_ticker,
        market_display=fallback_labels.market_display,
        selection_display=fallback_labels.selection_display,
        matchup_display=fallback_labels.matchup_display,
        contract_display=fallback_labels.contract_display,
        normalized_equivalent_display=fallback_labels.normalized_equivalent_display,
        side=position.contract_side,
        entry_price=float(position.entry_price),
        exit_price=None,
        current_price=float(current),
        current_price_updated_at=None,
        current_price_updated_at_display=None,
        quantity=position.quantity,
        profit_loss=float(pnl),
        profit_loss_percent=_float(pnl_percent),
        status=position.status,
        game_status="UNKNOWN",
        game_status_display="UNKNOWN",
        resolution=position.resolution,
        outcome=None,
    )


def _mapping_priority(mapping: MarketMapping | None) -> tuple[int, Decimal]:
    if mapping is None:
        return (99, Decimal("0"))
    status_priority = MAPPING_STATUS_PRIORITY.get(mapping.mapping_status, 50)
    return (status_priority, -(mapping.confidence or Decimal("0")))


def _date_bounds(day: date) -> tuple[datetime, datetime]:
    local_start = datetime.combine(day, time.min, tzinfo=get_dashboard_zone())
    start = ensure_aware_utc(local_start)
    return start, start + timedelta(days=1)


def _feature_status_summary(rows: list[MlbFeatureSnapshot]) -> tuple[dict[str, object], dict[str, object], list[str]]:
    source_statuses: dict[str, object] = {}
    module_counts: dict[str, dict[str, int]] = {}
    warnings: set[str] = set()
    for row in rows:
        statuses = row.source_statuses or {}
        for module_name, status in statuses.items():
            bucket = module_counts.setdefault(module_name, {})
            if isinstance(status, dict):
                values = [str(value) for value in status.values()]
                if values and all(value == "missing" for value in values):
                    aggregate = "missing"
                elif values and all(value == "available" for value in values):
                    aggregate = "available"
                elif any(value == "available" for value in values):
                    aggregate = "partial"
                else:
                    aggregate = "partial"
            else:
                aggregate = str(status)
            bucket[aggregate] = bucket.get(aggregate, 0) + 1

    for module_name in ("offense_season", "offense_recent", "starter_identity", "lineup", "park_weather"):
        counts = module_counts.get(module_name, {})
        if counts and _aggregate_module_status(counts) != "available":
            warnings.add(f"{module_name.upper()} MISSING OR DEGRADED")

    for module_name, counts in module_counts.items():
        source_statuses[module_name] = _aggregate_module_status(counts)

    return module_counts, source_statuses, sorted(warnings)


def _aggregate_module_status(counts: dict[str, int]) -> str:
    total = sum(counts.values())
    if total == 0:
        return "missing"
    if counts.get("missing", 0) == total:
        return "missing"
    if counts.get("missing", 0) > 0 or counts.get("partial", 0) > 0:
        return "partial"
    if counts.get("available", 0) > 0:
        return "available"
    return "partial"


def _module_status(source_statuses: dict[str, object], module_name: str) -> str:
    value = source_statuses.get(module_name)
    if isinstance(value, dict):
        statuses = {str(item) for item in value.values()}
        if "available" in statuses:
            return "available"
        if "partial" in statuses:
            return "partial"
        return "missing"
    return str(value or "missing")


def _family_scope(family: str | None, inning_scope: str | None = None) -> str:
    if inning_scope:
        return inning_scope
    return "first_five" if (family or "").startswith("first_five") else "full_game"


def _performance_bucket(trades: list[PaperTrade], key_fn) -> dict[str, dict[str, object]]:
    buckets: dict[str, list[PaperTrade]] = {}
    for trade in trades:
        buckets.setdefault(key_fn(trade), []).append(trade)
    result: dict[str, dict[str, object]] = {}
    for key, rows in buckets.items():
        wins = sum(1 for trade in rows if trade.outcome == "win" or (trade.realized_pnl or Decimal("0")) > 0)
        losses = sum(1 for trade in rows if trade.outcome == "loss" or (trade.realized_pnl or Decimal("0")) < 0)
        pushes = sum(1 for trade in rows if trade.outcome in {"push", "void"})
        realized = sum((trade.realized_pnl or Decimal("0")) for trade in rows)
        stake = sum(((trade.entry_price * trade.quantity) + paper_trade_fee(trade)) for trade in rows)
        result[key] = {
            "trades": len(rows),
            "win_rate": (wins / len(rows)) if rows else None,
            "roi": float(realized / stake) if stake else None,
            "profit_loss": float(realized),
            "record": f"{wins}-{losses}-{pushes}",
        }
    return result


def _decision_breakdown(candidates: list[ModelCandidate]) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    by_family: dict[str, dict[str, int]] = {}
    by_scope: dict[str, dict[str, int]] = {}
    for candidate in candidates:
        family = candidate.market_family or candidate.market_type or "unknown"
        scope = _family_scope(family, candidate.inning_scope)
        decision = candidate.decision or "unknown"
        by_family.setdefault(family, {})[decision] = by_family.setdefault(family, {}).get(decision, 0) + 1
        by_scope.setdefault(scope, {})[decision] = by_scope.setdefault(scope, {}).get(decision, 0) + 1
    return by_family, by_scope


def _latest_job_status(session: Session, epoch: PaperTradingEpoch) -> dict[str, JobRunSummary]:
    job_names = ["daily-setup", "candidate-sweep", "price-refresh", "settlement", "governance", "full-paper-cycle"]
    ranked = (
        select(
            JobRun.id.label("job_run_id"),
            func.row_number()
            .over(
                partition_by=JobRun.job_name,
                order_by=(JobRun.started_at.desc(), JobRun.id.desc()),
            )
            .label("job_rank"),
        )
        .where(JobRun.job_name.in_(job_names))
        .where(JobRun.paper_trading_epoch_id == epoch.id)
        .subquery()
    )
    rows = list(
        session.scalars(
            select(JobRun)
            .join(ranked, JobRun.id == ranked.c.job_run_id)
            .where(ranked.c.job_rank == 1)
            .order_by(JobRun.job_name.asc())
        )
    )
    latest: dict[str, JobRunSummary] = {}
    for row in rows:
        if row.job_name in latest:
            continue
        latest[row.job_name] = JobRunSummary(
            job_name=row.job_name,
            status=row.status,
            started_at=to_eastern_iso(row.started_at),
            completed_at=to_eastern_iso(row.completed_at),
            duration_seconds=row.duration_seconds,
            target_date=row.target_date.isoformat() if row.target_date else None,
            result=row.result or {},
        )
    return latest


def _websocket_status(session: Session) -> WebSocketStatusSummary:
    settings = get_settings()
    row = session.scalar(
        select(MarketDataWorkerStatus)
        .where(MarketDataWorkerStatus.status_key == "kalshi_ws_paper")
        .order_by(MarketDataWorkerStatus.id.desc())
        .limit(1)
    )
    if row is None:
        return WebSocketStatusSummary(
            enabled=settings.websocket_market_data_enabled,
            running=False,
            source="rest_fallback",
        )
    running = ws_status_running_is_fresh(row)
    return WebSocketStatusSummary(
        enabled=row.enabled,
        running=running,
        source=row.source if running else "rest_fallback",
        subscribed_market_count=row.subscribed_market_count,
        last_seen_at=to_eastern_iso(row.last_seen_at),
        last_message_at=to_eastern_iso(row.last_message_at),
        reconnect_count=row.reconnect_count,
        stale_count=row.stale_count,
        last_error=row.last_error,
    )


def dashboard_summary_from_db(
    session: Session,
    closed_date: date | None = None,
    *,
    epoch_key: str | None = None,
    include_archived: bool = False,
) -> DashboardSummary:
    selected_closed_date = closed_date or today_eastern()
    summary = empty_dashboard_summary(selected_closed_date)
    epoch_filter = resolve_epoch_filter(session, epoch_key=epoch_key, include_archived=include_archived)
    active_epoch = epoch_filter.epoch
    summary.active_epoch = ActiveEpochSummary(
        epoch_key=active_epoch.epoch_key,
        display_name=active_epoch.display_name,
        status=active_epoch.status,
        mode=active_epoch.mode,
        starting_balance=float(active_epoch.starting_balance),
        started_at=to_eastern_iso(active_epoch.started_at),
    )
    summary.paper_starting_balance = float(active_epoch.starting_balance)
    newest_snapshots = list(
        session.scalars(
            select(BalanceSnapshot)
            .where(BalanceSnapshot.paper_trading_epoch_id == active_epoch.id)
            .order_by(BalanceSnapshot.captured_at.desc())
            .limit(500)
        )
    )
    snapshots = list(reversed(newest_snapshots))
    summary.portfolio_series = [
        PortfolioPoint(timestamp=snapshot.captured_at, value=float(snapshot.portfolio_value)) for snapshot in snapshots
    ]
    if snapshots:
        latest_snapshot = snapshots[-1]
        summary.cash_balance = float(latest_snapshot.cash_balance)
        summary.portfolio_value = float(latest_snapshot.portfolio_value)
    else:
        totals = calculate_paper_portfolio(session, epoch=active_epoch)
        summary.cash_balance = float(totals.cash_balance)
        summary.portfolio_value = float(totals.portfolio_value)

    settled = list(
        session.scalars(
            select(PaperTrade)
            .where(PaperTrade.paper_trading_epoch_id == active_epoch.id)
            .where(PaperTrade.status.in_(["settled", "closed", "void"]))
        )
    )
    wins = sum(1 for trade in settled if trade.outcome == "win" or (trade.realized_pnl or Decimal("0")) > 0)
    losses = sum(1 for trade in settled if trade.outcome == "loss" or (trade.realized_pnl or Decimal("0")) < 0)
    pushes = sum(1 for trade in settled if trade.outcome in {"push", "void"})
    realized = sum((trade.realized_pnl or Decimal("0")) for trade in settled)
    stake = sum(((trade.entry_price * trade.quantity) + paper_trade_fee(trade)) for trade in settled)
    summary.performance = PerformanceMetrics(
        win_rate=(wins / len(settled)) if settled else None,
        roi=(float(realized / stake) if stake else None),
        profit_loss=float(realized),
        record=f"{wins}-{losses}-{pushes}",
    )

    open_positions = [] if not include_archived else list(session.scalars(select(Position).where(Position.status == "open").limit(100)))
    open_trade_rows = list(
        session.execute(
            select(PaperTrade, MlbGame, KalshiMarket)
            .outerjoin(ModelCandidate, PaperTrade.candidate_id == ModelCandidate.id)
            .outerjoin(MlbGame, ModelCandidate.mlb_game_id == MlbGame.id)
            .outerjoin(KalshiMarket, ModelCandidate.kalshi_market_id == KalshiMarket.id)
            .where(PaperTrade.paper_trading_epoch_id == active_epoch.id)
            .where(PaperTrade.status == "open")
            .limit(100)
        )
    )
    summary.positions = [_position_from_position(position) for position in open_positions]
    position_keys = {(position.market_ticker, position.contract_side) for position in open_positions}
    summary.positions.extend(
        _position_from_trade(trade, game, market)
        for trade, game, market in open_trade_rows
        if (trade.market_ticker, trade.contract_side) not in position_keys
    )
    closed_start, closed_end = _date_bounds(selected_closed_date)
    closed_trade_rows = list(
        session.execute(
            select(PaperTrade, MlbGame, KalshiMarket)
            .outerjoin(ModelCandidate, PaperTrade.candidate_id == ModelCandidate.id)
            .outerjoin(MlbGame, ModelCandidate.mlb_game_id == MlbGame.id)
            .outerjoin(KalshiMarket, ModelCandidate.kalshi_market_id == KalshiMarket.id)
            .where(PaperTrade.paper_trading_epoch_id == active_epoch.id)
            .where(PaperTrade.status.in_(["settled", "closed", "void"]))
            .where(
                or_(
                    (PaperTrade.exit_time >= closed_start) & (PaperTrade.exit_time < closed_end),
                    (PaperTrade.settled_at >= closed_start) & (PaperTrade.settled_at < closed_end),
                )
            )
            .order_by(PaperTrade.exit_time.desc().nullslast(), PaperTrade.settled_at.desc().nullslast())
            .limit(200)
        )
    )
    summary.closed_positions = [_position_from_trade(trade, game, market) for trade, game, market in closed_trade_rows]
    summary.closed_positions_date = selected_closed_date.isoformat()
    summary.closed_positions_count = len(summary.closed_positions)

    active_version = session.scalar(select(ModelVersion).where(ModelVersion.is_active.is_(True)))
    active_parameter_version = session.scalar(
        select(ModelParameterVersion).where(ModelParameterVersion.is_active.is_(True))
    )
    last_training, last_calibration, last_threshold = latest_governance_artifacts(session, active_epoch.id)
    last_prediction = session.scalar(
        select(ModelPredictionRun)
        .where(ModelPredictionRun.paper_trading_epoch_id == active_epoch.id)
        .where(ModelPredictionRun.target_date == today_eastern())
        .order_by(ModelPredictionRun.started_at.desc())
    )
    today_feature_rows = list(
        session.scalars(
            select(MlbFeatureSnapshot)
            .where(MlbFeatureSnapshot.target_date == today_eastern())
            .where(MlbFeatureSnapshot.source == FEATURE_VERSION)
            .order_by(MlbFeatureSnapshot.captured_at.desc())
            .limit(100)
        )
    )
    feature_completeness, source_statuses, critical_warnings = _feature_status_summary(today_feature_rows)
    candidate_count = (
        session.scalar(
            select(func.count(ModelCandidate.id)).where(ModelCandidate.paper_trading_epoch_id == active_epoch.id)
        )
        or 0
    )
    training_eligible_count = (
        session.scalar(
            select(func.count(ModelCandidate.id))
            .where(ModelCandidate.paper_trading_epoch_id == active_epoch.id)
            .where(ModelCandidate.training_eligible.is_(True))
        )
        or 0
    )
    resolved_mature_samples = (
        session.scalar(
            select(func.count(ModelCandidate.id))
            .where(ModelCandidate.paper_trading_epoch_id == active_epoch.id)
            .where(ModelCandidate.training_eligible.is_(True))
            .where(ModelCandidate.outcome.in_(["win", "loss"]))
            .where(ModelCandidate.feature_version == FEATURE_VERSION)
        )
        or 0
    )
    candidate_avg_data_quality = session.scalar(
        select(func.avg(ModelCandidate.data_quality)).where(ModelCandidate.feature_version == FEATURE_VERSION)
        .where(ModelCandidate.paper_trading_epoch_id == active_epoch.id)
    )
    active_candidates = list(
        session.scalars(
            select(ModelCandidate)
            .where(ModelCandidate.paper_trading_epoch_id == active_epoch.id)
            .order_by(ModelCandidate.evaluated_at.desc())
            .limit(1000)
        )
    )
    summary.decision_breakdown_by_family, summary.decision_breakdown_by_scope = _decision_breakdown(active_candidates)
    summary.performance_by_family = _performance_bucket(
        settled,
        lambda trade: trade.market_family or "unknown",
    )
    summary.performance_by_scope = _performance_bucket(
        settled,
        lambda trade: _family_scope(trade.market_family, trade.inning_scope),
    )
    if last_prediction and last_prediction.summary:
        summary.latest_candidate_diagnostics = dict(last_prediction.summary.get("candidate_diagnostics") or {})
    summary.job_status = _latest_job_status(session, active_epoch)
    summary.websocket_status = _websocket_status(session)
    feature_avg_data_quality = (
        sum((row.data_quality or Decimal("0")) for row in today_feature_rows) / Decimal(len(today_feature_rows))
        if today_feature_rows
        else None
    )
    avg_data_quality = candidate_avg_data_quality if candidate_avg_data_quality is not None else feature_avg_data_quality
    source_status = source_status_report(session)
    summary.model_status = ModelStatus(
        active_model_version=active_version.version_tag if active_version else None,
        active_parameter_version=active_parameter_version.version_tag if active_parameter_version else None,
        active_calibration_version=active_parameter_version.version_tag if active_parameter_version else None,
        feature_version=active_version.feature_version if active_version else None,
        calibration_status=last_calibration.status if last_calibration else "not_run",
        last_training_run=last_training.started_at if last_training else None,
        last_calibration_run=last_calibration.started_at if last_calibration else None,
        candidate_count=int(candidate_count),
        resolved_mature_samples=int(resolved_mature_samples),
        training_eligible_count=int(training_eligible_count),
        last_governance_status=last_training.status if last_training else "not_run",
        governance_status=last_training.status if last_training else "not_run",
        trade_policy=last_prediction.trade_policy if last_prediction and last_prediction.trade_policy else {},
        trade_caps_used=(
            {
                **((last_prediction.summary or {}).get("cap_counts", {}) if last_prediction else {}),
                **((last_prediction.summary or {}).get("risk_caps", {}) if last_prediction else {}),
                **((last_prediction.summary or {}).get("candidate_sweep_window", {}) if last_prediction else {}),
                **{
                    key: (last_prediction.summary or {}).get(key)
                    for key in ("candidates_yes", "candidates_no", "paper_trades_yes", "paper_trades_no")
                    if last_prediction and key in (last_prediction.summary or {})
                },
                "paper_trades": last_prediction.trades_created if last_prediction else 0,
            }
        ),
        trade_threshold_policy=last_threshold.thresholds if last_threshold else {},
        data_quality_summary={
            "avg": float(avg_data_quality) if avg_data_quality is not None else None,
            "feature_version": active_version.feature_version if active_version else None,
        },
        feature_completeness=feature_completeness,
        source_statuses=source_statuses,
        critical_module_warnings=critical_warnings,
        lineup_status=_module_status(source_statuses, "lineup"),
        starter_status=_module_status(source_statuses, "starter_identity"),
        weather_status=_module_status(source_statuses, "park_weather"),
        network_sources_enabled=bool(source_status["feature_sync_enable_network_sources"]),
        public_sources_enabled=bool(source_status["public_sources_enabled"]),
        last_feature_sync_status=dict(source_status["last_feature_sync_status"]),
        notes=[
            "PR3c fix2 run-distribution model is paper-only.",
            "Parameter promotion remains gated by resolved mature sample thresholds.",
        ],
    )
    summary.last_update = to_eastern_iso(utc_now())
    summary.last_update_display = eastern_display(utc_now())
    return summary


def today_bounds():
    day = today_eastern()
    local_start = datetime.combine(day, time.min, tzinfo=get_dashboard_zone())
    start = ensure_aware_utc(local_start)
    return day, start, start + timedelta(days=1)


def list_today_games(session: Session):
    _, start, end = today_bounds()
    return list(
        session.scalars(
            select(MlbGame).where(MlbGame.scheduled_start >= start).where(MlbGame.scheduled_start < end).order_by(MlbGame.scheduled_start)
        )
    )


def list_today_markets(session: Session):
    _, start, end = today_bounds()
    rows = list(
        session.execute(
            select(KalshiMarket, MarketMapping)
            .outerjoin(MarketMapping, KalshiMarket.id == MarketMapping.kalshi_market_id)
            .outerjoin(MlbGame, MarketMapping.mlb_game_id == MlbGame.id)
            .where(
                or_(
                    (KalshiMarket.occurrence_datetime >= start) & (KalshiMarket.occurrence_datetime < end),
                    (MlbGame.scheduled_start >= start) & (MlbGame.scheduled_start < end),
                )
            )
            .order_by(KalshiMarket.occurrence_datetime.asc().nullslast(), MlbGame.scheduled_start.asc().nullslast())
        )
    )
    deduped: dict[int, tuple[KalshiMarket, MarketMapping | None]] = {}
    for market, mapping in rows:
        existing = deduped.get(market.id)
        if existing is None or _mapping_priority(mapping) < _mapping_priority(existing[1]):
            deduped[market.id] = (market, mapping)

    return list(deduped.values())[:500]


def list_today_candidates(session: Session):
    _, start, end = today_bounds()
    active_epoch = resolve_epoch_filter(session).epoch
    return list(
        session.execute(
            select(ModelCandidate, MlbGame, KalshiMarket)
            .outerjoin(MlbGame, ModelCandidate.mlb_game_id == MlbGame.id)
            .outerjoin(KalshiMarket, ModelCandidate.kalshi_market_id == KalshiMarket.id)
            .where(ModelCandidate.paper_trading_epoch_id == active_epoch.id)
            .where(ModelCandidate.evaluated_at >= start)
            .where(ModelCandidate.evaluated_at < end)
            .order_by(ModelCandidate.evaluated_at.desc())
            .limit(500)
        )
    )
