from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    BalanceSnapshot,
    CalibrationRun,
    KalshiMarket,
    MarketMapping,
    MlbGame,
    ModelCandidate,
    ModelPredictionRun,
    ModelVersion,
    PaperTrade,
    Position,
    TrainingRun,
)
from app.schemas import (
    BotMode,
    DashboardSummary,
    ModelStatus,
    PerformanceMetrics,
    PortfolioPoint,
    PositionSummary,
)
from app.services.contracts import contract_labels, market_type_from_ticker
from app.services.portfolio import calculate_paper_portfolio
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
            feature_version=None,
            calibration_status="not_run",
            last_training_run=None,
            last_calibration_run=None,
            candidate_count=0,
            resolved_mature_samples=0,
            training_eligible_count=0,
            last_governance_status="not_run",
            trade_policy={},
            trade_caps_used={},
            data_quality_summary={},
            notes=["No mature model run has been recorded yet."],
        ),
        paper_starting_balance=float(settings.paper_starting_balance),
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
    pnl = (
        trade.realized_pnl
        if trade.realized_pnl is not None
        else ((current - trade.entry_price) * trade.quantity).quantize(Decimal("0.01"))
    )
    pnl_percent = ((pnl / (trade.entry_price * trade.quantity)).quantize(Decimal("0.0001"))) if trade.entry_price else None
    fallback_labels = contract_labels(
        game=game,
        market=market,
        market_ticker=trade.market_ticker,
        market_type=market_type_from_ticker(trade.market_ticker),
        selection_code=trade.selection_code or (market.selection_code if market else None),
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


def dashboard_summary_from_db(session: Session, closed_date: date | None = None) -> DashboardSummary:
    selected_closed_date = closed_date or today_eastern()
    summary = empty_dashboard_summary(selected_closed_date)
    newest_snapshots = list(
        session.scalars(select(BalanceSnapshot).order_by(BalanceSnapshot.captured_at.desc()).limit(500))
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
        totals = calculate_paper_portfolio(session)
        summary.cash_balance = float(totals.cash_balance)
        summary.portfolio_value = float(totals.portfolio_value)

    settled = list(session.scalars(select(PaperTrade).where(PaperTrade.status.in_(["settled", "closed", "void"]))))
    wins = sum(1 for trade in settled if trade.outcome == "win" or (trade.realized_pnl or Decimal("0")) > 0)
    losses = sum(1 for trade in settled if trade.outcome == "loss" or (trade.realized_pnl or Decimal("0")) < 0)
    pushes = sum(1 for trade in settled if trade.outcome in {"push", "void"})
    realized = sum((trade.realized_pnl or Decimal("0")) for trade in settled)
    stake = sum((trade.entry_price * trade.quantity) for trade in settled)
    summary.performance = PerformanceMetrics(
        win_rate=(wins / len(settled)) if settled else None,
        roi=(float(realized / stake) if stake else None),
        profit_loss=float(realized),
        record=f"{wins}-{losses}-{pushes}",
    )

    open_positions = list(session.scalars(select(Position).where(Position.status == "open").limit(100)))
    open_trade_rows = list(
        session.execute(
            select(PaperTrade, MlbGame, KalshiMarket)
            .outerjoin(ModelCandidate, PaperTrade.candidate_id == ModelCandidate.id)
            .outerjoin(MlbGame, ModelCandidate.mlb_game_id == MlbGame.id)
            .outerjoin(KalshiMarket, ModelCandidate.kalshi_market_id == KalshiMarket.id)
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
    last_training = session.scalar(select(TrainingRun).order_by(TrainingRun.started_at.desc()))
    last_calibration = session.scalar(select(CalibrationRun).order_by(CalibrationRun.started_at.desc()))
    last_prediction = session.scalar(
        select(ModelPredictionRun)
        .where(ModelPredictionRun.target_date == today_eastern())
        .order_by(ModelPredictionRun.started_at.desc())
    )
    candidate_count = session.scalar(select(func.count(ModelCandidate.id))) or 0
    training_eligible_count = (
        session.scalar(select(func.count(ModelCandidate.id)).where(ModelCandidate.training_eligible.is_(True))) or 0
    )
    resolved_mature_samples = (
        session.scalar(
            select(func.count(ModelCandidate.id))
            .where(ModelCandidate.training_eligible.is_(True))
            .where(ModelCandidate.outcome.in_(["win", "loss"]))
            .where(ModelCandidate.feature_version == "mature_mlb_features_v1")
        )
        or 0
    )
    avg_data_quality = session.scalar(
        select(func.avg(ModelCandidate.data_quality)).where(ModelCandidate.feature_version == "mature_mlb_features_v1")
    )
    summary.model_status = ModelStatus(
        active_model_version=active_version.version_tag if active_version else None,
        feature_version=active_version.feature_version if active_version else None,
        calibration_status=last_calibration.status if last_calibration else "not_run",
        last_training_run=last_training.started_at if last_training else None,
        last_calibration_run=last_calibration.started_at if last_calibration else None,
        candidate_count=int(candidate_count),
        resolved_mature_samples=int(resolved_mature_samples),
        training_eligible_count=int(training_eligible_count),
        last_governance_status=last_training.status if last_training else "not_run",
        trade_policy=last_prediction.trade_policy if last_prediction and last_prediction.trade_policy else {},
        trade_caps_used=(
            {
                **((last_prediction.summary or {}).get("cap_counts", {}) if last_prediction else {}),
                "paper_trades": last_prediction.trades_created if last_prediction else 0,
            }
        ),
        data_quality_summary={
            "avg": float(avg_data_quality) if avg_data_quality is not None else None,
            "feature_version": active_version.feature_version if active_version else None,
        },
        notes=[
            "PR3c mature run-distribution model is paper-only.",
            "Calibration remains conservative until resolved mature sample thresholds are met.",
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
    return list(
        session.execute(
            select(ModelCandidate, MlbGame, KalshiMarket)
            .outerjoin(MlbGame, ModelCandidate.mlb_game_id == MlbGame.id)
            .outerjoin(KalshiMarket, ModelCandidate.kalshi_market_id == KalshiMarket.id)
            .where(ModelCandidate.evaluated_at >= start)
            .where(ModelCandidate.evaluated_at < end)
            .order_by(ModelCandidate.evaluated_at.desc())
            .limit(500)
        )
    )
