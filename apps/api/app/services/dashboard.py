from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import logging
import time as perf_time
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, load_only

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
    ObservationFilterSummary,
    PerformanceMetrics,
    PortfolioPoint,
    PositionSummary,
    ActiveEpochSummary,
    JobRunSummary,
    WebSocketStatusSummary,
)
from app.services.contracts import contract_labels, market_type_from_ticker
from app.services.features import FEATURE_VERSION, source_status_report, starter_status_report
from app.services.modeling import governance_status as model_governance_status
from app.services.portfolio import calculate_paper_portfolio, paper_trade_fee
from app.services.paper_epoch import resolve_epoch_filter
from app.services.risk_governance import RISK_GOVERNANCE_FIELD_NAMES
from app.services.ws_market_data import ws_status_running_is_fresh
from app.time_utils import eastern_display, ensure_aware_utc, get_dashboard_zone, to_eastern_iso, today_eastern, utc_now

MAPPING_STATUS_PRIORITY = {"confirmed": 0, "candidate": 1, "needs_review": 2}
OBSERVATION_START_DATE = date(2026, 7, 2)
OBSERVATION_TIME_ZONE = ZoneInfo("America/New_York")
OBSERVATION_FILTER_REASON = "default_dashboard_excludes_pre_2026_07_02_validation_rows"
PORTFOLIO_SERIES_MAX_POINTS = 500
PORTFOLIO_SERIES_SOURCE_SNAPSHOTS = "active_epoch_balance_snapshots"
PORTFOLIO_SERIES_SOURCE_FILTERED_TOTALS = "observation_filtered_portfolio_totals"
PORTFOLIO_SERIES_FALLBACK_NO_SNAPSHOTS = "no_usable_active_epoch_balance_snapshots"
PORTFOLIO_SERIES_FALLBACK_UNSAFE_PRE_OBSERVATION_TRADES = "pre_observation_trades_can_affect_snapshot_series"
logger = logging.getLogger(__name__)


def empty_dashboard_summary(closed_date: date | None = None) -> DashboardSummary:
    settings = get_settings()
    selected_closed_date = closed_date or today_eastern()
    observation_start = _observation_start_at()
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
        observation_filter=ObservationFilterSummary(
            active=True,
            include_pre_observation=False,
            observation_start_date=OBSERVATION_START_DATE.isoformat(),
            observation_start_at=to_eastern_iso(observation_start) or observation_start.isoformat(),
            observation_start_display=eastern_display(observation_start) or observation_start.isoformat(),
            reason=OBSERVATION_FILTER_REASON,
        ),
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


def _compact_position_spread_audit(candidate: ModelCandidate | None) -> dict[str, object] | None:
    scoring_rationale = candidate.__dict__.get("scoring_rationale") if candidate is not None else None
    spread = scoring_rationale.get("spread_verification") if isinstance(scoring_rationale, dict) else None
    if not isinstance(spread, dict):
        return None
    keys = (
        "audit_status",
        "verified",
        "selection_code",
        "line_value",
        "inning_scope",
        "settlement_formula",
        "no_is_true_complement",
        "complement_safe_for_paper_settlement",
        "push_possible",
        "push_rule_verified",
    )
    compact = {key: spread.get(key) for key in keys if key in spread}
    return compact or None


def _position_rationale(candidate: ModelCandidate, trade: PaperTrade) -> dict[str, object]:
    scoring_rationale = candidate.__dict__.get("scoring_rationale")
    risk_limit_basis = scoring_rationale.get("risk_limit_basis") if isinstance(scoring_rationale, dict) else None
    rationale: dict[str, object] = {
        "decision": candidate.decision,
        "probability_edge": _float(candidate.probability_edge),
        "net_expected_value": _float(candidate.net_expected_value),
        "data_quality": _float(candidate.data_quality),
        "risk_limit_basis": risk_limit_basis,
        "contracts": trade.quantity,
        "estimated_total_cost": _float(trade.estimated_total_cost),
        "total_fee_estimate": _float(trade.total_fee_estimate),
    }
    spread_audit = _compact_position_spread_audit(candidate)
    if spread_audit is not None:
        rationale["spread_audit"] = spread_audit
    return rationale


def _position_exposure_payload(trade: PaperTrade) -> dict[str, object] | None:
    payload = {
        "economic_exposure_label": trade.economic_exposure_label,
        "economic_exposure_key": trade.economic_exposure_key,
        "economic_exposure_family": trade.economic_exposure_family,
        "economic_exposure_scope": trade.economic_exposure_scope,
        "economic_exposure_direction": trade.economic_exposure_direction,
        "economic_exposure_team": trade.economic_exposure_team,
        "economic_exposure_line": _float(trade.economic_exposure_line),
        "contract_mechanics_label": trade.contract_mechanics_label,
        "concept_cluster_key": trade.concept_cluster_key,
        "same_game_concept_cluster_key": trade.same_game_concept_cluster_key,
        "line_class": trade.line_class,
        "line_class_reason": trade.line_class_reason,
        "line_ladder_rank": trade.line_ladder_rank,
        "line_ladder_distance_from_central": trade.line_ladder_distance_from_central,
        "line_ladder_size": trade.line_ladder_size,
    }
    compact = {key: value for key, value in payload.items() if value is not None}
    return compact or None


def _position_selector_payload(trade: PaperTrade) -> dict[str, object] | None:
    payload = {
        "selector_policy_version": trade.selector_policy_version,
        "selector_mode": trade.selector_mode,
        "selector_status": trade.selector_status,
        "selector_decision": trade.selector_decision,
        "selector_rejection_reason": trade.selector_rejection_reason,
        "selector_threshold_profile": trade.selector_threshold_profile,
        "selector_min_net_ev": _float(trade.selector_min_net_ev),
        "selector_min_prob_edge": _float(trade.selector_min_prob_edge),
        "selector_min_data_quality": _float(trade.selector_min_data_quality),
        "selector_line_class_policy": trade.selector_line_class_policy,
        "selector_concept_cluster_key": trade.selector_concept_cluster_key,
        "selector_same_game_concept_cluster_key": trade.selector_same_game_concept_cluster_key,
        "selector_cluster_rank": trade.selector_cluster_rank,
        "selector_cluster_rank_score": _float(trade.selector_cluster_rank_score),
        "selector_selected_from_cluster": trade.selector_selected_from_cluster,
        "selector_shadow_only": trade.selector_shadow_only,
        "selector_live_like_eligible_before_cluster": trade.selector_live_like_eligible_before_cluster,
        "selector_live_like_eligible_after_cluster": trade.selector_live_like_eligible_after_cluster,
    }
    compact = {key: value for key, value in payload.items() if value is not None}
    return compact or None


def _position_risk_governance_payload(trade: PaperTrade) -> dict[str, object] | None:
    payload = {key: getattr(trade, key, None) for key in RISK_GOVERNANCE_FIELD_NAMES}
    payload["risk_governance_rank_score"] = _float(trade.risk_governance_rank_score)
    compact = {key: value for key, value in payload.items() if value is not None}
    return compact or None


def _position_from_trade(
    trade: PaperTrade,
    game: MlbGame | None = None,
    market: KalshiMarket | None = None,
    candidate: ModelCandidate | None = None,
) -> PositionSummary:
    current = _first_decimal(trade.exit_price, trade.current_price, trade.entry_price)
    quantity = Decimal(trade.quantity)
    fee = paper_trade_fee(trade)
    entry_notional = (trade.entry_price * quantity).quantize(Decimal("0.01"))
    cost_basis = (entry_notional + fee).quantize(Decimal("0.01"))
    current_value = (current * quantity).quantize(Decimal("0.01"))
    exit_value = (trade.exit_price * quantity).quantize(Decimal("0.01")) if trade.exit_price is not None else None
    pnl = (
        trade.realized_pnl
        if trade.realized_pnl is not None
        else (current_value - cost_basis).quantize(Decimal("0.01"))
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
    exposure_label = trade.economic_exposure_label
    mechanics_label = trade.contract_mechanics_label or trade.contract_display
    display = exposure_label or mechanics_label or trade.market_display or fallback_labels.contract_display
    selected_rationale = _position_rationale(candidate, trade) if candidate is not None else {}
    exposure_payload = _position_exposure_payload(trade)
    if exposure_payload is not None:
        selected_rationale["exposure_taxonomy"] = exposure_payload
    selector_payload = _position_selector_payload(trade)
    if selector_payload is not None:
        selected_rationale["selector"] = selector_payload
    risk_governance_payload = _position_risk_governance_payload(trade)
    if risk_governance_payload is not None:
        selected_rationale["risk_governance"] = risk_governance_payload
    return PositionSummary(
        time_entered=to_eastern_iso(trade.entry_time),
        time_entered_display=eastern_display(trade.entry_time),
        time_closed=to_eastern_iso(trade.exit_time or trade.settled_at),
        time_closed_display=eastern_display(trade.exit_time or trade.settled_at),
        market=display,
        market_ticker=trade.market_ticker,
        market_display=exposure_label or trade.market_display or fallback_labels.market_display,
        selection_display=trade.selection_display or fallback_labels.selection_display,
        matchup_display=trade.matchup_display or fallback_labels.matchup_display,
        contract_display=display,
        normalized_equivalent_display=fallback_labels.normalized_equivalent_display,
        economic_exposure_label=trade.economic_exposure_label,
        economic_exposure_key=trade.economic_exposure_key,
        economic_exposure_family=trade.economic_exposure_family,
        economic_exposure_scope=trade.economic_exposure_scope,
        economic_exposure_direction=trade.economic_exposure_direction,
        economic_exposure_team=trade.economic_exposure_team,
        economic_exposure_line=_float(trade.economic_exposure_line),
        contract_mechanics_label=trade.contract_mechanics_label,
        concept_cluster_key=trade.concept_cluster_key,
        same_game_concept_cluster_key=trade.same_game_concept_cluster_key,
        line_class=trade.line_class,
        line_class_reason=trade.line_class_reason,
        line_ladder_rank=trade.line_ladder_rank,
        line_ladder_distance_from_central=trade.line_ladder_distance_from_central,
        line_ladder_size=trade.line_ladder_size,
        selector_policy_version=trade.selector_policy_version,
        selector_mode=trade.selector_mode,
        selector_status=trade.selector_status,
        selector_decision=trade.selector_decision,
        selector_rejection_reason=trade.selector_rejection_reason,
        selector_threshold_profile=trade.selector_threshold_profile,
        selector_min_net_ev=_float(trade.selector_min_net_ev),
        selector_min_prob_edge=_float(trade.selector_min_prob_edge),
        selector_min_data_quality=_float(trade.selector_min_data_quality),
        selector_line_class_policy=trade.selector_line_class_policy,
        selector_concept_cluster_key=trade.selector_concept_cluster_key,
        selector_same_game_concept_cluster_key=trade.selector_same_game_concept_cluster_key,
        selector_cluster_rank=trade.selector_cluster_rank,
        selector_cluster_rank_score=_float(trade.selector_cluster_rank_score),
        selector_selected_from_cluster=trade.selector_selected_from_cluster,
        selector_shadow_only=trade.selector_shadow_only,
        selector_live_like_eligible_before_cluster=trade.selector_live_like_eligible_before_cluster,
        selector_live_like_eligible_after_cluster=trade.selector_live_like_eligible_after_cluster,
        risk_governance_policy_version=trade.risk_governance_policy_version,
        risk_governance_enabled=trade.risk_governance_enabled,
        risk_governance_status=trade.risk_governance_status,
        risk_governance_decision=trade.risk_governance_decision,
        risk_governance_rejection_reason=trade.risk_governance_rejection_reason,
        risk_governance_family_status=trade.risk_governance_family_status,
        risk_governance_family_cap_status=trade.risk_governance_family_cap_status,
        risk_governance_concept_cluster_cap_status=trade.risk_governance_concept_cluster_cap_status,
        risk_governance_same_game_cap_status=trade.risk_governance_same_game_cap_status,
        risk_governance_alternate_line_cap_status=trade.risk_governance_alternate_line_cap_status,
        risk_governance_low_price_tail_cap_status=trade.risk_governance_low_price_tail_cap_status,
        risk_governance_drawdown_status=trade.risk_governance_drawdown_status,
        risk_governance_approved_before_caps=trade.risk_governance_approved_before_caps,
        risk_governance_approved_after_caps=trade.risk_governance_approved_after_caps,
        risk_governance_shadow_only=trade.risk_governance_shadow_only,
        risk_governance_blocked=trade.risk_governance_blocked,
        risk_governance_rank=trade.risk_governance_rank,
        risk_governance_rank_score=_float(trade.risk_governance_rank_score),
        display_title=fallback_labels.display_title,
        display_subtitle=fallback_labels.display_subtitle,
        raw_ticker_display=fallback_labels.raw_ticker_display,
        selected_position_rationale=selected_rationale,
        side=trade.contract_side,
        entry_price=float(trade.entry_price),
        exit_price=float(trade.exit_price) if trade.exit_price is not None else None,
        current_price=float(current),
        entry_notional=float(entry_notional),
        entry_total_cost=float(cost_basis),
        current_value=float(current_value),
        exit_value=float(exit_value) if exit_value is not None else None,
        fee_paid=_float(trade.fee_paid),
        estimated_fee=_float(trade.total_fee_estimate if trade.total_fee_estimate is not None else fee),
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
    quantity = Decimal(position.quantity)
    entry_notional = (position.entry_price * quantity).quantize(Decimal("0.01"))
    current_value = (current * quantity).quantize(Decimal("0.01"))
    pnl = (current_value - entry_notional).quantize(Decimal("0.01"))
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
        display_title=fallback_labels.display_title,
        display_subtitle=fallback_labels.display_subtitle,
        raw_ticker_display=fallback_labels.raw_ticker_display,
        selected_position_rationale={},
        side=position.contract_side,
        entry_price=float(position.entry_price),
        exit_price=None,
        current_price=float(current),
        entry_notional=float(entry_notional),
        entry_total_cost=float(entry_notional),
        current_value=float(current_value),
        exit_value=None,
        fee_paid=None,
        estimated_fee=0.0,
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


def _observation_start_at() -> datetime:
    local_start = datetime.combine(
        OBSERVATION_START_DATE,
        time.min,
        tzinfo=OBSERVATION_TIME_ZONE,
    )
    return ensure_aware_utc(local_start)


def _apply_trade_observation_filter(statement, include_pre_observation: bool, cutoff: datetime):
    if include_pre_observation:
        return statement
    return statement.where(PaperTrade.entry_time >= cutoff)


def _apply_candidate_observation_filter(statement, include_pre_observation: bool, cutoff: datetime):
    if include_pre_observation:
        return statement
    return statement.where(ModelCandidate.evaluated_at >= cutoff)


def _filtered_portfolio_totals(
    *,
    starting_balance: Decimal,
    trades: list[PaperTrade],
) -> tuple[Decimal, Decimal]:
    realized = sum((trade.realized_pnl or Decimal("0")) for trade in trades if trade.status != "open") or Decimal("0")
    open_trades = [trade for trade in trades if trade.status == "open"]
    open_cost = sum(
        (trade.entry_price * Decimal(trade.quantity)) + paper_trade_fee(trade) for trade in open_trades
    ) or Decimal("0")
    open_mark = sum(
        (trade.current_price if trade.current_price is not None else trade.entry_price) * Decimal(trade.quantity)
        for trade in open_trades
    ) or Decimal("0")
    cash = (starting_balance + realized - open_cost).quantize(Decimal("0.01"))
    portfolio = (cash + open_mark).quantize(Decimal("0.01"))
    return cash, portfolio


def _excluded_pre_observation_trades_can_affect_snapshot_series(
    session: Session,
    *,
    active_epoch: PaperTradingEpoch,
    cutoff: datetime,
) -> bool:
    return bool(
        session.scalar(
            select(func.count(PaperTrade.id))
            .where(PaperTrade.paper_trading_epoch_id == active_epoch.id)
            .where(PaperTrade.entry_time < cutoff)
            .where(
                or_(
                    PaperTrade.status == "open",
                    PaperTrade.exit_time >= cutoff,
                    PaperTrade.settled_at >= cutoff,
                    PaperTrade.current_price_updated_at >= cutoff,
                )
            )
        )
    )


def _portfolio_point_from_snapshot_row(row) -> PortfolioPoint:
    return PortfolioPoint(
        timestamp=row.captured_at,
        value=float(row.portfolio_value),
        cash_balance=_float(getattr(row, "cash_balance", None)),
        snapshot_id=int(row.id),
        source=getattr(row, "source", None),
        snapshot_type=getattr(row, "snapshot_type", None),
    )


def _same_portfolio_snapshot_values(left, right) -> bool:
    return left.portfolio_value == right.portfolio_value and left.cash_balance == right.cash_balance


def _coalesce_no_change_portfolio_rows(rows: list[object]) -> list[object]:
    if len(rows) <= 2:
        return rows
    coalesced = [rows[0]]
    for row in rows[1:-1]:
        if _same_portfolio_snapshot_values(coalesced[-1], row):
            continue
        coalesced.append(row)
    if int(rows[-1].id) != int(coalesced[-1].id):
        coalesced.append(rows[-1])
    return coalesced


def _offset_money(value: float, offset: Decimal) -> float:
    return float((Decimal(str(value)) + offset).quantize(Decimal("0.01")))


def _offset_portfolio_series(
    points: list[PortfolioPoint],
    *,
    portfolio_offset: Decimal,
    cash_offset: Decimal,
) -> list[PortfolioPoint]:
    if not points or (portfolio_offset == 0 and cash_offset == 0):
        return points
    return [
        PortfolioPoint(
            timestamp=point.timestamp,
            value=_offset_money(point.value, portfolio_offset),
            cash_balance=_offset_money(point.cash_balance, cash_offset) if point.cash_balance is not None else None,
            snapshot_id=point.snapshot_id,
            source=point.source,
            snapshot_type=point.snapshot_type,
        )
        for point in points
    ]


def _portfolio_snapshot_sort_key(row) -> tuple[datetime, int]:
    return row.captured_at, int(row.id)


def _portfolio_snapshot_bucket_index(row, first_row, latest_row, bucket_count: int) -> int:
    elapsed_seconds = (latest_row.captured_at - first_row.captured_at).total_seconds()
    if elapsed_seconds <= 0:
        return 0
    row_elapsed = (row.captured_at - first_row.captured_at).total_seconds()
    return min(max(int((row_elapsed / elapsed_seconds) * bucket_count), 0), bucket_count - 1)


def _compact_portfolio_snapshot_rows(
    rows: Iterable[object],
    first_row,
    latest_row,
) -> tuple[list[PortfolioPoint], bool]:
    if first_row is None or latest_row is None:
        return [], False
    exact_rows: list[object] = []
    keep_by_id = {int(first_row.id): first_row, int(latest_row.id): latest_row}
    bucket_count = max(1, (PORTFOLIO_SERIES_MAX_POINTS - 2) // 2)
    buckets: dict[int, dict[str, object]] = {}
    row_count = 0
    for row in rows:
        row_count += 1
        if row_count <= PORTFOLIO_SERIES_MAX_POINTS:
            exact_rows.append(row)
        bucket_index = _portfolio_snapshot_bucket_index(row, first_row, latest_row, bucket_count)
        bucket = buckets.setdefault(bucket_index, {})
        low = bucket.get("low")
        high = bucket.get("high")
        if low is None or (row.portfolio_value, row.captured_at, row.id) < (
            low.portfolio_value,
            low.captured_at,
            low.id,
        ):
            bucket["low"] = row
        if high is None or (row.portfolio_value, row.captured_at, row.id) > (
            high.portfolio_value,
            high.captured_at,
            high.id,
        ):
            bucket["high"] = row

    if row_count <= PORTFOLIO_SERIES_MAX_POINTS:
        return [_portfolio_point_from_snapshot_row(row) for row in _coalesce_no_change_portfolio_rows(exact_rows)], False

    for bucket in buckets.values():
        for row in bucket.values():
            keep_by_id[int(row.id)] = row
    compact_rows = sorted(keep_by_id.values(), key=_portfolio_snapshot_sort_key)
    compact_rows = _coalesce_no_change_portfolio_rows(compact_rows[:PORTFOLIO_SERIES_MAX_POINTS])
    return [_portfolio_point_from_snapshot_row(row) for row in compact_rows], True


def _observation_filter_summary(
    session: Session,
    *,
    active_epoch: PaperTradingEpoch,
    selected_closed_date: date,
    include_pre_observation: bool,
    cutoff: datetime,
) -> ObservationFilterSummary:
    closed_start, closed_end = _date_bounds(selected_closed_date)
    excluded_total = (
        session.scalar(
            select(func.count(PaperTrade.id))
            .where(PaperTrade.paper_trading_epoch_id == active_epoch.id)
            .where(PaperTrade.entry_time < cutoff)
        )
        or 0
    )
    excluded_closed = (
        session.scalar(
            select(func.count(PaperTrade.id))
            .where(PaperTrade.paper_trading_epoch_id == active_epoch.id)
            .where(PaperTrade.status.in_(["settled", "closed", "void"]))
            .where(PaperTrade.entry_time < cutoff)
            .where(
                or_(
                    (PaperTrade.exit_time >= closed_start) & (PaperTrade.exit_time < closed_end),
                    (PaperTrade.settled_at >= closed_start) & (PaperTrade.settled_at < closed_end),
                )
            )
        )
        or 0
    )
    return ObservationFilterSummary(
        active=not include_pre_observation,
        include_pre_observation=include_pre_observation,
        observation_start_date=OBSERVATION_START_DATE.isoformat(),
        observation_start_at=to_eastern_iso(cutoff) or cutoff.isoformat(),
        observation_start_display=eastern_display(cutoff) or cutoff.isoformat(),
        excluded_pre_observation_count=int(excluded_total),
        excluded_pre_observation_closed_count=int(excluded_closed),
        historical_rows_available=bool(excluded_total),
        reason=OBSERVATION_FILTER_REASON,
    )


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


def _decision_breakdown_from_db(
    session: Session,
    *,
    epoch_id: int,
    include_pre_observation: bool,
    cutoff: datetime,
) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    started_at = perf_time.perf_counter()
    statement = _apply_candidate_observation_filter(
        select(
            ModelCandidate.market_family,
            ModelCandidate.market_type,
            ModelCandidate.inning_scope,
            ModelCandidate.decision,
            func.count(ModelCandidate.id).label("candidate_count"),
        )
        .where(ModelCandidate.paper_trading_epoch_id == epoch_id)
        .group_by(
            ModelCandidate.market_family,
            ModelCandidate.market_type,
            ModelCandidate.inning_scope,
            ModelCandidate.decision,
        ),
        include_pre_observation,
        cutoff,
    )
    by_family: dict[str, dict[str, int]] = {}
    by_scope: dict[str, dict[str, int]] = {}
    row_count = 0
    candidate_total = 0
    for row in session.execute(statement):
        row_count += 1
        family = row.market_family or row.market_type or "unknown"
        scope = _family_scope(family, row.inning_scope)
        decision = row.decision or "unknown"
        count = int(row.candidate_count or 0)
        candidate_total += count
        by_family.setdefault(family, {})[decision] = by_family.setdefault(family, {}).get(decision, 0) + count
        by_scope.setdefault(scope, {})[decision] = by_scope.setdefault(scope, {}).get(decision, 0) + count
    logger.info(
        "dashboard_decision_breakdown_counts epoch_id=%s duration_ms=%s grouped_rows=%s candidate_total=%s include_pre_observation=%s",
        epoch_id,
        int((perf_time.perf_counter() - started_at) * 1000),
        row_count,
        candidate_total,
        include_pre_observation,
    )
    return by_family, by_scope


ALWAYS_OMIT_PAYLOAD_KEYS = {
    "features",
    "raw_contract_text",
    "raw_output",
    "raw_payload",
    "rationale",
}

HEAVY_PAYLOAD_KEYS = {
    "candidate_ids",
    "counterfactuals",
    "items",
    "rows",
    "source_health",
    "source_inventory",
    "tables",
    "top_counterfactual_candidates_blocked_by_quality",
    "top_deduped_counterfactual_opinions_by_game_scope_family",
}


def _json_scalar(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _int_json_scalar(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return int(value)
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(Decimal(value.strip('"')))
        except Exception:
            return None
    return None


def _payload_count(value: object) -> int | None:
    if isinstance(value, (list, tuple, set, dict)):
        return len(value)
    return None


def _compact_payload(
    value: object,
    *,
    max_depth: int = 3,
    max_list_items: int = 5,
    max_dict_items: int = 40,
    include_heavy: bool = False,
) -> object:
    if value is None or isinstance(value, (bool, int, float, str, Decimal, date, datetime)):
        return _json_scalar(value)
    if max_depth <= 0:
        count = _payload_count(value)
        return {"truncated": True, "item_count": count} if count is not None else _json_scalar(value)
    if isinstance(value, (list, tuple, set)):
        rows = list(value)
        return {
            "item_count": len(rows),
            "items": [
                _compact_payload(
                    item,
                    max_depth=max_depth - 1,
                    max_list_items=max_list_items,
                    max_dict_items=max_dict_items,
                    include_heavy=include_heavy,
                )
                for item in rows[:max_list_items]
            ],
            "truncated": len(rows) > max_list_items,
        }
    if isinstance(value, dict):
        compact: dict[str, object] = {}
        for index, (raw_key, raw_item) in enumerate(value.items()):
            if index >= max_dict_items:
                compact["truncated"] = True
                compact["omitted_key_count"] = len(value) - max_dict_items
                break
            key = str(raw_key)
            if key in ALWAYS_OMIT_PAYLOAD_KEYS:
                compact["omitted_blob_field_count"] = int(compact.get("omitted_blob_field_count", 0)) + 1
                continue
            if not include_heavy and key in HEAVY_PAYLOAD_KEYS:
                count = _payload_count(raw_item)
                if count is not None:
                    compact[f"{key}_count"] = count
                compact[f"{key}_omitted"] = True
                continue
            compact[key] = _compact_payload(
                raw_item,
                max_depth=max_depth - 1,
                max_list_items=max_list_items,
                max_dict_items=max_dict_items,
                include_heavy=include_heavy,
            )
        return compact
    return _json_scalar(value)


def _compact_job_result(result: dict[str, object] | None, *, include_details: bool = False) -> dict[str, object]:
    if not result:
        return {}
    return dict(
        _compact_payload(
            result,
            max_depth=5 if include_details else 3,
            max_list_items=25 if include_details else 5,
            max_dict_items=80 if include_details else 40,
            include_heavy=include_details,
        )
    )


def _compact_governance_registry(registry: object, *, include_details: bool = False) -> dict[str, object]:
    if not isinstance(registry, dict):
        return {}
    if include_details:
        return dict(_compact_payload(registry, max_depth=5, max_list_items=25, max_dict_items=80, include_heavy=True))
    return {
        key: _json_scalar(value)
        for key, value in registry.items()
        if key.endswith("_count") or key in {"policy"}
    }


def _compact_latest_errors(errors: object, *, limit: int = 3) -> tuple[int, list[object]]:
    if not isinstance(errors, list):
        return 0, []
    return len(errors), [
        _compact_payload(error, max_depth=2, max_list_items=3, max_dict_items=12, include_heavy=False)
        for error in errors[:limit]
    ]


def compact_source_status_payload(source_status: dict[str, object], *, include_details: bool = False) -> dict[str, object]:
    if include_details:
        return dict(
            _compact_payload(
                source_status,
                max_depth=5,
                max_list_items=25,
                max_dict_items=120,
                include_heavy=True,
            )
        )
    health = source_status.get("source_health")
    health_rows = health if isinstance(health, list) else []
    health_status_counts: dict[str, int] = {}
    health_criticality_counts: dict[str, int] = {}
    for row in health_rows:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "unknown")
        criticality = str(row.get("criticality") or "unknown")
        health_status_counts[status] = health_status_counts.get(status, 0) + 1
        health_criticality_counts[criticality] = health_criticality_counts.get(criticality, 0) + 1
    latest_error_count, latest_error_sample = _compact_latest_errors(source_status.get("latest_errors"))
    completeness = source_status.get("latest_feature_completeness")
    completeness_summary = (
        (completeness or {}).get("summary") if isinstance(completeness, dict) else None
    )
    return {
        "feature_sync_enable_network_sources": bool(source_status.get("feature_sync_enable_network_sources")),
        "public_sources_enabled": bool(source_status.get("public_sources_enabled")),
        "validation_status": source_status.get("validation_status"),
        "last_attempted_sync": source_status.get("last_attempted_sync"),
        "source_count": len(health_rows),
        "source_health_status_counts": health_status_counts,
        "source_health_criticality_counts": health_criticality_counts,
        "latest_error_count": latest_error_count,
        "latest_errors_sample": latest_error_sample,
        "pybaseball_fangraphs_status": source_status.get("pybaseball_fangraphs_status"),
        "advanced_public_stats_status": source_status.get("advanced_public_stats_status"),
        "statcast_savant_status": source_status.get("statcast_savant_status"),
        "last_feature_sync_status": _compact_payload(
            source_status.get("last_feature_sync_status") or {},
            max_depth=2,
            max_list_items=3,
            max_dict_items=20,
            include_heavy=False,
        ),
        "latest_feature_completeness": {
            "date": completeness.get("date") if isinstance(completeness, dict) else None,
            "summary": completeness_summary if isinstance(completeness_summary, dict) else {},
        },
    }


CANDIDATE_DATA_QUALITY_COMPACT_KEYS = (
    "raw_feature_snapshot_data_quality_avg",
    "raw_feature_snapshot_data_quality_max",
    "paper_observation_data_quality_avg",
    "paper_observation_data_quality_max",
    "quality_threshold",
    "candidate_stage_market_context_status_counts",
    "quality_block_reason_counts",
)

CANDIDATE_DIAGNOSTIC_COMPACT_KEYS = (
    "candidates_total",
    "trade_eligible_before_quality",
    "trade_eligible_after_quality",
    "blocked_by_quality_only",
    "would_pass_ev_if_quality_allowed",
    "would_pass_edge_if_quality_allowed",
    "ev_edge_pass_but_quality_fail",
    "blocked_by_ev",
    "blocked_by_edge",
    "blocked_by_price",
    "blocked_by_mapping",
    "blocked_by_caps",
    "average_data_quality",
    "paper_observation_data_quality_avg",
    "quality_threshold",
    "quality_block_reason_counts",
    "candidate_stage_market_context_status_counts",
)

QUALITY_EV_DIAGNOSTIC_COMPACT_KEYS = (
    "candidates_total",
    "ev_pass_count",
    "edge_pass_count",
    "ev_and_edge_pass_count",
    "pre_quality_trade_eligible_count",
    "post_quality_trade_eligible_count",
    "quality_blocked_count",
    "unique_game_scope_family_count",
    "deduped_ev_edge_pass_count_by_game_scope_family",
    "deduped_pre_quality_trade_eligible_count_by_game_scope_family",
    "counts_by_quality_bucket",
)

SELECTOR_DIAGNOSTIC_COMPACT_KEYS = (
    "selector_policy_version",
    "selector_mode",
    "selector_candidates_considered",
    "selector_pre_cluster_eligible",
    "selector_selected_after_cluster",
    "selector_rejected_by_family_scope_threshold",
    "selector_rejected_by_line_class",
    "selector_rejected_by_concept_cluster",
    "selector_shadow_only_count",
    "selector_by_family_scope",
    "selector_by_line_class",
    "selector_by_concept_cluster_sample",
)

PROBABILITY_ADAPTER_DIAGNOSTIC_COMPACT_KEYS = (
    "probability_adapter_policy_version",
    "probability_adapter_counts",
    "probability_adapter_calibration_hook_counts",
    "probability_adapter_family_counts",
    "probability_adapter_missing_count",
    "probability_adapter_error_count",
    "probability_adapter_error_reason_counts",
    "probability_adapter_error_family_counts",
    "probability_adapter_errors_excluded_from_governance_training",
)

PROBABILITY_HARDENING_DIAGNOSTIC_COMPACT_KEYS = (
    "probability_hardening_policy_version",
    "probability_hardening_enabled",
    "probability_hardening_line_class_policy",
    "probability_hardening_applied_count",
    "probability_hardening_shadow_only_count",
    "probability_hardening_block_recommendation_count",
    "probability_hardening_error_count",
    "probability_hardening_missing_count",
    "probability_hardening_status_counts",
    "probability_hardening_reason_counts",
    "probability_hardening_by_line_class",
    "probability_hardening_by_family_scope",
    "probability_hardening_consistency_status_counts",
    "probability_hardening_monotonicity_status_counts",
)

RISK_GOVERNANCE_DIAGNOSTIC_COMPACT_KEYS = (
    "risk_governance_policy_version",
    "risk_governance_enabled",
    "risk_candidates_considered",
    "risk_approved_before_caps",
    "risk_approved_after_caps",
    "risk_shadow_only_count",
    "risk_blocked_count",
    "risk_rejected_by_family_status",
    "risk_rejected_by_family_cap",
    "risk_rejected_by_concept_cluster_cap",
    "risk_rejected_by_same_game_cap",
    "risk_rejected_by_alternate_line_cap",
    "risk_rejected_by_low_price_tail_cap",
    "risk_rejected_by_drawdown_halt",
    "risk_by_family_scope",
    "risk_by_line_class",
    "risk_by_same_game_sample",
    "risk_drawdown_summary",
    "candidate_risk_governance_field_counts",
    "trade_eligible_after_risk_governance",
    "trades_blocked_by_risk_governance",
)


def _prediction_summary_json_value(section: str, key: str):
    return ModelPredictionRun.summary[section][key]


def _prediction_summary_compact_columns() -> list[object]:
    columns: list[object] = [
        _prediction_summary_json_value("candidate_diagnostics", key).label(f"candidate_{key}")
        for key in sorted(set(CANDIDATE_DATA_QUALITY_COMPACT_KEYS + CANDIDATE_DIAGNOSTIC_COMPACT_KEYS))
    ]
    columns.extend(
        _prediction_summary_json_value("quality_ev_diagnostics", key).label(f"quality_ev_{key}")
        for key in QUALITY_EV_DIAGNOSTIC_COMPACT_KEYS
    )
    columns.extend(ModelPredictionRun.summary[key].label(f"selector_{key}") for key in SELECTOR_DIAGNOSTIC_COMPACT_KEYS)
    columns.extend(
        ModelPredictionRun.summary[key].label(f"probability_adapter_{key}")
        for key in PROBABILITY_ADAPTER_DIAGNOSTIC_COMPACT_KEYS
    )
    columns.extend(
        ModelPredictionRun.summary[key].label(f"probability_hardening_{key}")
        for key in PROBABILITY_HARDENING_DIAGNOSTIC_COMPACT_KEYS
    )
    columns.extend(
        ModelPredictionRun.summary[key].label(f"risk_governance_{key}")
        for key in RISK_GOVERNANCE_DIAGNOSTIC_COMPACT_KEYS
    )
    columns.extend(
        [
            func.json_array_length(
                _prediction_summary_json_value("candidate_diagnostics", "top_quality_blockers")
            ).label("candidate_top_quality_blockers_count"),
            func.json_array_length(
                _prediction_summary_json_value(
                    "quality_ev_diagnostics",
                    "top_counterfactual_candidates_blocked_by_quality",
                )
            ).label("quality_ev_top_counterfactual_candidates_blocked_by_quality_count"),
            func.json_array_length(
                _prediction_summary_json_value(
                    "quality_ev_diagnostics",
                    "top_deduped_counterfactual_opinions_by_game_scope_family",
                )
            ).label("quality_ev_top_deduped_counterfactual_opinions_by_game_scope_family_count"),
        ]
    )
    return columns


def _compact_prediction_summary_from_row(row) -> dict[str, object] | None:
    if not row:
        return None
    candidate = {
        key: row[f"candidate_{key}"]
        for key in sorted(set(CANDIDATE_DATA_QUALITY_COMPACT_KEYS + CANDIDATE_DIAGNOSTIC_COMPACT_KEYS))
        if row.get(f"candidate_{key}") is not None
    }
    top_blockers_count = row.get("candidate_top_quality_blockers_count")
    if isinstance(top_blockers_count, int):
        candidate["top_quality_blockers_count"] = top_blockers_count
    quality_ev = {
        key: row[f"quality_ev_{key}"]
        for key in QUALITY_EV_DIAGNOSTIC_COMPACT_KEYS
        if row.get(f"quality_ev_{key}") is not None
    }
    for row_key, target_key in (
        (
            "quality_ev_top_counterfactual_candidates_blocked_by_quality_count",
            "top_counterfactual_candidates_blocked_by_quality_count",
        ),
        (
            "quality_ev_top_deduped_counterfactual_opinions_by_game_scope_family_count",
            "top_deduped_counterfactual_opinions_by_game_scope_family_count",
        ),
    ):
        value = row.get(row_key)
        if isinstance(value, int):
            quality_ev[target_key] = value
    selector = {
        key: row[f"selector_{key}"]
        for key in SELECTOR_DIAGNOSTIC_COMPACT_KEYS
        if row.get(f"selector_{key}") is not None
    }
    probability_adapter = {
        key: row[f"probability_adapter_{key}"]
        for key in PROBABILITY_ADAPTER_DIAGNOSTIC_COMPACT_KEYS
        if row.get(f"probability_adapter_{key}") is not None
    }
    probability_hardening = {
        key: row[f"probability_hardening_{key}"]
        for key in PROBABILITY_HARDENING_DIAGNOSTIC_COMPACT_KEYS
        if row.get(f"probability_hardening_{key}") is not None
    }
    risk_governance = {
        key: row[f"risk_governance_{key}"]
        for key in RISK_GOVERNANCE_DIAGNOSTIC_COMPACT_KEYS
        if row.get(f"risk_governance_{key}") is not None
    }
    if (
        not candidate
        and not quality_ev
        and not selector
        and not probability_adapter
        and not probability_hardening
        and not risk_governance
    ):
        return None
    return {
        "candidate_diagnostics": candidate,
        "quality_ev_diagnostics": quality_ev,
        "selector": selector,
        "probability_adapter": probability_adapter,
        "probability_hardening": probability_hardening,
        "risk_governance": risk_governance,
    }


def _compact_candidate_data_quality(
    run_summary: dict[str, object] | None,
    *,
    include_details: bool = False,
) -> dict[str, object]:
    diagnostics = (run_summary or {}).get("candidate_diagnostics") if run_summary else None
    if not isinstance(diagnostics, dict):
        return {}
    result = {
        key: diagnostics.get(key)
        for key in CANDIDATE_DATA_QUALITY_COMPACT_KEYS
        if key in diagnostics
    }
    blockers = diagnostics.get("top_quality_blockers")
    if isinstance(blockers, list):
        result["top_quality_blockers_count"] = len(blockers)
        if include_details:
            result["top_quality_blockers"] = _compact_payload(
                blockers,
                max_depth=3,
                max_list_items=10,
                max_dict_items=20,
                include_heavy=False,
            )
    elif isinstance(diagnostics.get("top_quality_blockers_count"), int):
        result["top_quality_blockers_count"] = diagnostics["top_quality_blockers_count"]
    return result


def _compact_candidate_diagnostics(
    run_summary: dict[str, object] | None,
    *,
    include_details: bool = False,
) -> dict[str, object]:
    if not run_summary:
        return {}
    candidate = run_summary.get("candidate_diagnostics")
    quality_ev = run_summary.get("quality_ev_diagnostics")
    selector = run_summary.get("selector")
    if not isinstance(selector, dict):
        selector = {key: run_summary.get(key) for key in SELECTOR_DIAGNOSTIC_COMPACT_KEYS if key in run_summary}
    probability_adapter = run_summary.get("probability_adapter")
    if not isinstance(probability_adapter, dict):
        probability_adapter = {
            key: run_summary.get(key)
            for key in PROBABILITY_ADAPTER_DIAGNOSTIC_COMPACT_KEYS
            if key in run_summary
        }
    probability_hardening = run_summary.get("probability_hardening")
    if not isinstance(probability_hardening, dict):
        probability_hardening = {
            key: run_summary.get(key)
            for key in PROBABILITY_HARDENING_DIAGNOSTIC_COMPACT_KEYS
            if key in run_summary
        }
    risk_governance = run_summary.get("risk_governance")
    if not isinstance(risk_governance, dict):
        risk_governance = {
            key: run_summary.get(key)
            for key in RISK_GOVERNANCE_DIAGNOSTIC_COMPACT_KEYS
            if key in run_summary
        }
    if include_details:
        return dict(
            _compact_payload(
                {
                    "candidate_diagnostics": candidate or {},
                    "quality_ev_diagnostics": quality_ev or {},
                    "selector": selector or {},
                    "probability_adapter": probability_adapter or {},
                    "probability_hardening": probability_hardening or {},
                    "risk_governance": risk_governance or {},
                },
                max_depth=5,
                max_list_items=25,
                max_dict_items=100,
                include_heavy=True,
            )
        )
    result: dict[str, object] = {}
    if isinstance(candidate, dict):
        result["candidate_diagnostics"] = {
            key: candidate.get(key)
            for key in CANDIDATE_DIAGNOSTIC_COMPACT_KEYS
            if key in candidate
        }
        blockers = candidate.get("top_quality_blockers")
        if isinstance(blockers, list):
            result["candidate_diagnostics"]["top_quality_blockers_count"] = len(blockers)
        elif isinstance(candidate.get("top_quality_blockers_count"), int):
            result["candidate_diagnostics"]["top_quality_blockers_count"] = candidate["top_quality_blockers_count"]
    if isinstance(quality_ev, dict):
        result["quality_ev_diagnostics"] = {
            key: quality_ev.get(key)
            for key in QUALITY_EV_DIAGNOSTIC_COMPACT_KEYS
            if key in quality_ev
        }
        for key in (
            "top_counterfactual_candidates_blocked_by_quality",
            "top_deduped_counterfactual_opinions_by_game_scope_family",
        ):
            value = quality_ev.get(key)
            if isinstance(value, list):
                result["quality_ev_diagnostics"][f"{key}_count"] = len(value)
            elif isinstance(quality_ev.get(f"{key}_count"), int):
                result["quality_ev_diagnostics"][f"{key}_count"] = quality_ev[f"{key}_count"]
    if isinstance(selector, dict) and selector:
        result["selector"] = {
            key: selector.get(key)
            for key in SELECTOR_DIAGNOSTIC_COMPACT_KEYS
            if key in selector
        }
    if probability_adapter:
        result["probability_adapter"] = probability_adapter
    if probability_hardening:
        result["probability_hardening"] = probability_hardening
    if risk_governance:
        result["risk_governance"] = risk_governance
    return result


def _latest_job_status(
    session: Session,
    epoch: PaperTradingEpoch,
    *,
    include_job_results: bool = False,
    detailed_job_names: set[str] | None = None,
) -> dict[str, JobRunSummary]:
    job_names = [
        "daily-setup",
        "candidate-sweep",
        "price-refresh",
        "settlement",
        "governance",
        "full-paper-cycle",
        "spread-audit",
    ]
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
    compact_rows = list(
        session.execute(
            select(
                JobRun.id,
                JobRun.job_name,
                JobRun.status,
                JobRun.started_at,
                JobRun.completed_at,
                JobRun.duration_seconds,
                JobRun.target_date,
            )
            .join(ranked, JobRun.id == ranked.c.job_run_id)
            .where(ranked.c.job_rank == 1)
            .order_by(JobRun.job_name.asc())
        )
    )
    latest = {
        row.job_name: JobRunSummary(
            job_name=row.job_name,
            status=row.status,
            started_at=to_eastern_iso(row.started_at),
            completed_at=to_eastern_iso(row.completed_at),
            duration_seconds=row.duration_seconds,
            target_date=row.target_date.isoformat() if row.target_date else None,
            result_is_compact=True,
            step_count=None,
            warning_count=None,
            error_count=None,
            result={},
        )
        for row in compact_rows
    }

    detailed_names = {row.job_name for row in compact_rows} if include_job_results else (detailed_job_names or set())
    detailed_ids = [row.id for row in compact_rows if row.job_name in detailed_names]
    if not detailed_ids:
        return latest

    rows = list(
        session.scalars(
            select(JobRun).where(JobRun.id.in_(detailed_ids)).order_by(JobRun.job_name.asc())
        )
    )
    for row in rows:
        latest[row.job_name] = JobRunSummary(
            job_name=row.job_name,
            status=row.status,
            started_at=to_eastern_iso(row.started_at),
            completed_at=to_eastern_iso(row.completed_at),
            duration_seconds=row.duration_seconds,
            target_date=row.target_date.isoformat() if row.target_date else None,
            result_is_compact=False,
            step_count=len(row.steps or []),
            warning_count=len(row.warnings or []),
            error_count=len(row.errors or []),
            result=_compact_job_result(row.result, include_details=True),
        )
    return latest


def _latest_spread_audit_counts(session: Session, epoch: PaperTradingEpoch) -> dict[str, object]:
    spread_audit_result = JobRun.result["spread_audit"]
    row = session.execute(
        select(
            JobRun.status.label("job_status"),
            JobRun.started_at,
            JobRun.completed_at,
            JobRun.target_date,
            spread_audit_result["checked"].label("checked"),
            spread_audit_result["verified"].label("verified"),
            spread_audit_result["trusted_audit_only_count"].label("trusted_audit_only_count"),
            spread_audit_result["needs_review_count"].label("needs_review_count"),
            spread_audit_result["unsafe_count"].label("unsafe_count"),
            spread_audit_result["parse_error_count"].label("parse_error_count"),
            spread_audit_result["settlement_text_unverified_count"].label("settlement_text_unverified_count"),
            spread_audit_result["push_behavior_uncertain_count"].label("push_behavior_uncertain_count"),
            spread_audit_result["paper_trades_created"].label("paper_trades_created"),
            spread_audit_result["read_only"].label("read_only"),
        )
        .where(JobRun.job_name == "spread-audit")
        .where(JobRun.paper_trading_epoch_id == epoch.id)
        .order_by(JobRun.started_at.desc(), JobRun.id.desc())
        .limit(1)
    ).mappings().first()
    if not row:
        return {}
    compact: dict[str, object] = {
        "job_status": row["job_status"],
        "started_at": to_eastern_iso(row["started_at"]),
        "completed_at": to_eastern_iso(row["completed_at"]),
        "target_date": row["target_date"].isoformat() if row["target_date"] else None,
    }
    for key in (
        "checked",
        "verified",
        "trusted_audit_only_count",
        "needs_review_count",
        "unsafe_count",
        "parse_error_count",
        "settlement_text_unverified_count",
        "push_behavior_uncertain_count",
        "paper_trades_created",
    ):
        value = _int_json_scalar(row[key])
        if value is not None:
            compact[key] = value
    read_only = row["read_only"]
    if isinstance(read_only, bool):
        compact["read_only"] = read_only
    elif isinstance(read_only, int):
        compact["read_only"] = bool(read_only)
    elif isinstance(read_only, str):
        compact["read_only"] = read_only.strip('"').lower() == "true"
    return compact


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
    include_pre_observation: bool = False,
    include_diagnostics: bool = False,
    include_job_results: bool = False,
    include_source_details: bool = False,
    include_governance_details: bool = False,
    include_candidate_diagnostics: bool = False,
    include_spread_audit_details: bool = False,
) -> DashboardSummary:
    selected_closed_date = closed_date or today_eastern()
    summary = empty_dashboard_summary(selected_closed_date)
    epoch_filter = resolve_epoch_filter(session, epoch_key=epoch_key, include_archived=include_archived)
    active_epoch = epoch_filter.epoch
    observation_cutoff = _observation_start_at()
    summary.observation_filter = _observation_filter_summary(
        session,
        active_epoch=active_epoch,
        selected_closed_date=selected_closed_date,
        include_pre_observation=include_pre_observation,
        cutoff=observation_cutoff,
    )
    summary.active_epoch = ActiveEpochSummary(
        epoch_key=active_epoch.epoch_key,
        display_name=active_epoch.display_name,
        status=active_epoch.status,
        mode=active_epoch.mode,
        starting_balance=float(active_epoch.starting_balance),
        started_at=to_eastern_iso(active_epoch.started_at),
    )
    summary.paper_starting_balance = float(active_epoch.starting_balance)
    snapshot_columns = (
        BalanceSnapshot.id.label("id"),
        BalanceSnapshot.captured_at.label("captured_at"),
        BalanceSnapshot.cash_balance.label("cash_balance"),
        BalanceSnapshot.portfolio_value.label("portfolio_value"),
        BalanceSnapshot.source.label("source"),
        BalanceSnapshot.snapshot_type.label("snapshot_type"),
    )
    snapshot_query = (
        select(*snapshot_columns)
        .where(BalanceSnapshot.paper_trading_epoch_id == active_epoch.id)
        .order_by(BalanceSnapshot.captured_at.asc(), BalanceSnapshot.id.asc())
    )
    if not include_pre_observation:
        snapshot_query = snapshot_query.where(BalanceSnapshot.captured_at >= observation_cutoff)
    latest_snapshot_query = select(*snapshot_columns).where(BalanceSnapshot.paper_trading_epoch_id == active_epoch.id)
    if not include_pre_observation:
        latest_snapshot_query = latest_snapshot_query.where(BalanceSnapshot.captured_at >= observation_cutoff)
    first_snapshot = session.execute(
        latest_snapshot_query.order_by(BalanceSnapshot.captured_at.asc(), BalanceSnapshot.id.asc()).limit(1)
    ).first()
    latest_snapshot = session.execute(
        latest_snapshot_query.order_by(BalanceSnapshot.captured_at.desc(), BalanceSnapshot.id.desc()).limit(1)
    ).first()
    snapshot_rows = session.execute(snapshot_query.execution_options(yield_per=250, stream_results=True))
    snapshots, portfolio_series_truncated = _compact_portfolio_snapshot_rows(
        snapshot_rows,
        first_snapshot,
        latest_snapshot,
    )
    filtered_trades = list(
        session.scalars(
            _apply_trade_observation_filter(
                select(PaperTrade).where(PaperTrade.paper_trading_epoch_id == active_epoch.id),
                include_pre_observation,
                observation_cutoff,
            )
        )
    )
    filtered_cash_balance: Decimal | None = None
    filtered_portfolio_value: Decimal | None = None
    if not include_pre_observation:
        filtered_cash_balance, filtered_portfolio_value = _filtered_portfolio_totals(
            starting_balance=active_epoch.starting_balance,
            trades=filtered_trades,
        )
    snapshot_series_fallback_reason = (
        PORTFOLIO_SERIES_FALLBACK_UNSAFE_PRE_OBSERVATION_TRADES
        if (
            snapshots
            and not include_pre_observation
            and summary.observation_filter.excluded_pre_observation_count
            and _excluded_pre_observation_trades_can_affect_snapshot_series(
                session,
                active_epoch=active_epoch,
                cutoff=observation_cutoff,
            )
        )
        else None
    )
    summary.portfolio_series_active_epoch_id = active_epoch.id
    if snapshots and snapshot_series_fallback_reason is None:
        if (
            not include_pre_observation
            and summary.observation_filter.excluded_pre_observation_count
            and latest_snapshot is not None
            and filtered_cash_balance is not None
            and filtered_portfolio_value is not None
        ):
            snapshots = _offset_portfolio_series(
                snapshots,
                portfolio_offset=filtered_portfolio_value - latest_snapshot.portfolio_value,
                cash_offset=filtered_cash_balance - latest_snapshot.cash_balance,
            )
        summary.portfolio_series = snapshots
        summary.portfolio_series_source = PORTFOLIO_SERIES_SOURCE_SNAPSHOTS
        summary.portfolio_series_truncated = portfolio_series_truncated
        summary.portfolio_series_preserves_intraday_fluctuations = len(snapshots) >= 3
    else:
        now = utc_now()
        fallback_start = ensure_aware_utc(active_epoch.started_at)
        if not include_pre_observation and fallback_start < observation_cutoff:
            fallback_start = observation_cutoff
        if include_pre_observation:
            fallback_totals = calculate_paper_portfolio(session, epoch=active_epoch)
            fallback_value = fallback_totals.portfolio_value
        else:
            fallback_value = filtered_portfolio_value if filtered_portfolio_value is not None else active_epoch.starting_balance
        summary.portfolio_series = [
            PortfolioPoint(timestamp=fallback_start, value=float(active_epoch.starting_balance)),
            PortfolioPoint(timestamp=now, value=float(fallback_value)),
        ]
        summary.portfolio_series_source = PORTFOLIO_SERIES_SOURCE_FILTERED_TOTALS
        summary.portfolio_series_truncated = False
        summary.portfolio_series_preserves_intraday_fluctuations = False
        summary.portfolio_series_fallback_reason = snapshot_series_fallback_reason or PORTFOLIO_SERIES_FALLBACK_NO_SNAPSHOTS
    summary.portfolio_series_point_count = len(summary.portfolio_series)
    if summary.portfolio_series:
        summary.portfolio_series_started_at = to_eastern_iso(summary.portfolio_series[0].timestamp)
        summary.portfolio_series_ended_at = to_eastern_iso(summary.portfolio_series[-1].timestamp)
    if latest_snapshot and include_pre_observation:
        summary.cash_balance = float(latest_snapshot.cash_balance)
        summary.portfolio_value = float(latest_snapshot.portfolio_value)
    elif latest_snapshot and not summary.observation_filter.excluded_pre_observation_count:
        summary.cash_balance = float(latest_snapshot.cash_balance)
        summary.portfolio_value = float(latest_snapshot.portfolio_value)
    else:
        if include_pre_observation:
            totals = calculate_paper_portfolio(session, epoch=active_epoch)
            summary.cash_balance = float(totals.cash_balance)
            summary.portfolio_value = float(totals.portfolio_value)
        else:
            cash_balance = filtered_cash_balance if filtered_cash_balance is not None else active_epoch.starting_balance
            portfolio_value = filtered_portfolio_value if filtered_portfolio_value is not None else active_epoch.starting_balance
            summary.cash_balance = float(cash_balance)
            summary.portfolio_value = float(portfolio_value)

    settled = [trade for trade in filtered_trades if trade.status in {"settled", "closed", "void"}]
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

    open_position_query = select(Position).where(Position.status == "open")
    if not include_pre_observation:
        open_position_query = open_position_query.where(Position.opened_at >= observation_cutoff)
    open_positions = [] if not include_archived else list(session.scalars(open_position_query.limit(100)))
    open_trade_statement = (
        select(PaperTrade, MlbGame, KalshiMarket, ModelCandidate)
        .outerjoin(ModelCandidate, PaperTrade.candidate_id == ModelCandidate.id)
        .outerjoin(MlbGame, ModelCandidate.mlb_game_id == MlbGame.id)
        .outerjoin(KalshiMarket, ModelCandidate.kalshi_market_id == KalshiMarket.id)
        .options(
            load_only(
                ModelCandidate.id,
                ModelCandidate.decision,
                ModelCandidate.probability_edge,
                ModelCandidate.net_expected_value,
                ModelCandidate.data_quality,
                ModelCandidate.scoring_rationale,
            ),
            load_only(
                MlbGame.id,
                MlbGame.status,
                MlbGame.home_team,
                MlbGame.away_team,
                MlbGame.home_abbreviation,
                MlbGame.away_abbreviation,
            ),
        )
        .where(PaperTrade.paper_trading_epoch_id == active_epoch.id)
        .where(PaperTrade.status == "open")
    )
    open_trade_statement = _apply_trade_observation_filter(
        open_trade_statement,
        include_pre_observation,
        observation_cutoff,
    )
    open_trade_rows = list(
        session.execute(
            open_trade_statement.limit(100)
        )
    )
    summary.positions = [_position_from_position(position) for position in open_positions]
    position_keys = {(position.market_ticker, position.contract_side) for position in open_positions}
    summary.positions.extend(
        _position_from_trade(trade, game, market, candidate)
        for trade, game, market, candidate in open_trade_rows
        if (trade.market_ticker, trade.contract_side) not in position_keys
    )
    closed_start, closed_end = _date_bounds(selected_closed_date)
    closed_trade_statement = (
        select(PaperTrade, MlbGame, KalshiMarket, ModelCandidate)
        .outerjoin(ModelCandidate, PaperTrade.candidate_id == ModelCandidate.id)
        .outerjoin(MlbGame, ModelCandidate.mlb_game_id == MlbGame.id)
        .outerjoin(KalshiMarket, ModelCandidate.kalshi_market_id == KalshiMarket.id)
        .options(
            load_only(
                ModelCandidate.id,
                ModelCandidate.decision,
                ModelCandidate.probability_edge,
                ModelCandidate.net_expected_value,
                ModelCandidate.data_quality,
                ModelCandidate.scoring_rationale,
            ),
            load_only(
                MlbGame.id,
                MlbGame.status,
                MlbGame.home_team,
                MlbGame.away_team,
                MlbGame.home_abbreviation,
                MlbGame.away_abbreviation,
            ),
        )
        .where(PaperTrade.paper_trading_epoch_id == active_epoch.id)
        .where(PaperTrade.status.in_(["settled", "closed", "void"]))
        .where(
            or_(
                (PaperTrade.exit_time >= closed_start) & (PaperTrade.exit_time < closed_end),
                (PaperTrade.settled_at >= closed_start) & (PaperTrade.settled_at < closed_end),
            )
        )
    )
    closed_trade_statement = _apply_trade_observation_filter(
        closed_trade_statement,
        include_pre_observation,
        observation_cutoff,
    )
    closed_trade_rows = list(
        session.execute(
            closed_trade_statement.order_by(
                PaperTrade.exit_time.desc().nullslast(),
                PaperTrade.settled_at.desc().nullslast(),
            ).limit(200)
        )
    )
    summary.closed_positions = [
        _position_from_trade(trade, game, market, candidate) for trade, game, market, candidate in closed_trade_rows
    ]
    summary.closed_positions_date = selected_closed_date.isoformat()
    summary.closed_positions_count = len(summary.closed_positions)

    active_version = session.scalar(select(ModelVersion).where(ModelVersion.is_active.is_(True)))
    active_parameter_version = session.scalar(
        select(ModelParameterVersion).where(ModelParameterVersion.is_active.is_(True))
    )
    include_governance_registry_details = include_diagnostics or include_governance_details
    governance_summary = model_governance_status(
        session,
        active_epoch.id,
        include_details=include_governance_registry_details,
    )
    include_candidate_details = include_diagnostics or include_candidate_diagnostics
    last_prediction = session.execute(
        select(
            ModelPredictionRun.id,
            ModelPredictionRun.trades_created,
            ModelPredictionRun.trade_policy,
            ModelPredictionRun.summary["cap_counts"].label("summary_cap_counts"),
            ModelPredictionRun.summary["risk_caps"].label("summary_risk_caps"),
            ModelPredictionRun.summary["candidate_sweep_window"].label("summary_candidate_sweep_window"),
            ModelPredictionRun.summary["candidates_yes"].label("summary_candidates_yes"),
            ModelPredictionRun.summary["candidates_no"].label("summary_candidates_no"),
            ModelPredictionRun.summary["paper_trades_yes"].label("summary_paper_trades_yes"),
            ModelPredictionRun.summary["paper_trades_no"].label("summary_paper_trades_no"),
            *_prediction_summary_compact_columns(),
        )
        .where(ModelPredictionRun.paper_trading_epoch_id == active_epoch.id)
        .where(ModelPredictionRun.target_date == today_eastern())
        .order_by(ModelPredictionRun.started_at.desc())
    ).mappings().first()
    last_prediction_summary = (
        session.scalar(select(ModelPredictionRun.summary).where(ModelPredictionRun.id == last_prediction["id"]))
        if last_prediction and include_candidate_details
        else _compact_prediction_summary_from_row(last_prediction)
    )
    today_feature_rows = list(
        session.execute(
            select(MlbFeatureSnapshot.source_statuses, MlbFeatureSnapshot.data_quality)
            .where(MlbFeatureSnapshot.target_date == today_eastern())
            .where(MlbFeatureSnapshot.source == FEATURE_VERSION)
            .order_by(MlbFeatureSnapshot.captured_at.desc())
            .limit(100)
        )
    )
    feature_completeness, source_statuses, critical_warnings = _feature_status_summary(today_feature_rows)
    candidate_count_statement = _apply_candidate_observation_filter(
        select(func.count(ModelCandidate.id)).where(ModelCandidate.paper_trading_epoch_id == active_epoch.id),
        include_pre_observation,
        observation_cutoff,
    )
    candidate_count = session.scalar(candidate_count_statement) or 0
    training_eligible_statement = _apply_candidate_observation_filter(
        select(func.count(ModelCandidate.id))
        .where(ModelCandidate.paper_trading_epoch_id == active_epoch.id)
        .where(ModelCandidate.training_eligible.is_(True)),
        include_pre_observation,
        observation_cutoff,
    )
    training_eligible_count = session.scalar(training_eligible_statement) or 0
    candidate_avg_data_quality = session.scalar(
        _apply_candidate_observation_filter(
            select(func.avg(ModelCandidate.data_quality))
            .where(ModelCandidate.feature_version == FEATURE_VERSION)
            .where(ModelCandidate.paper_trading_epoch_id == active_epoch.id),
            include_pre_observation,
            observation_cutoff,
        )
    )
    summary.decision_breakdown_by_family, summary.decision_breakdown_by_scope = _decision_breakdown_from_db(
        session,
        epoch_id=active_epoch.id,
        include_pre_observation=include_pre_observation,
        cutoff=observation_cutoff,
    )
    summary.performance_by_family = _performance_bucket(
        settled,
        lambda trade: trade.market_family or "unknown",
    )
    summary.performance_by_scope = _performance_bucket(
        settled,
        lambda trade: _family_scope(trade.market_family, trade.inning_scope),
    )
    if last_prediction_summary:
        summary.latest_candidate_diagnostics = _compact_candidate_diagnostics(
            last_prediction_summary,
            include_details=include_candidate_details,
        )
    trade_caps_used: dict[str, object] = {
        "paper_trades": int(last_prediction["trades_created"]) if last_prediction else 0,
    }
    if last_prediction:
        for key in ("summary_cap_counts", "summary_risk_caps", "summary_candidate_sweep_window"):
            value = last_prediction[key]
            if isinstance(value, dict):
                trade_caps_used.update(value)
        for source_key, target_key in (
            ("summary_candidates_yes", "candidates_yes"),
            ("summary_candidates_no", "candidates_no"),
            ("summary_paper_trades_yes", "paper_trades_yes"),
            ("summary_paper_trades_no", "paper_trades_no"),
        ):
            value = last_prediction[source_key]
            if value is not None:
                trade_caps_used[target_key] = value
    if last_prediction_summary:
        trade_caps_used.update(last_prediction_summary.get("cap_counts", {}))
        trade_caps_used.update(last_prediction_summary.get("risk_caps", {}))
        trade_caps_used.update(last_prediction_summary.get("candidate_sweep_window", {}))
        risk_governance = last_prediction_summary.get("risk_governance")
        if isinstance(risk_governance, dict):
            trade_caps_used.update(
                {
                    key: risk_governance.get(key)
                    for key in RISK_GOVERNANCE_DIAGNOSTIC_COMPACT_KEYS
                    if key in risk_governance
                }
            )
        trade_caps_used.update(
            {
                key: last_prediction_summary.get(key)
                for key in ("candidates_yes", "candidates_no", "paper_trades_yes", "paper_trades_no")
                if key in last_prediction_summary
            }
        )
    summary.job_status = _latest_job_status(
        session,
        active_epoch,
        include_job_results=include_diagnostics or include_job_results,
        detailed_job_names={"spread-audit"} if include_spread_audit_details else None,
    )
    summary.websocket_status = _websocket_status(session)
    feature_avg_data_quality = (
        sum((row.data_quality or Decimal("0")) for row in today_feature_rows) / Decimal(len(today_feature_rows))
        if today_feature_rows
        else None
    )
    avg_data_quality = candidate_avg_data_quality if candidate_avg_data_quality is not None else feature_avg_data_quality
    source_status = source_status_report(session)
    compact_source_status = compact_source_status_payload(
        source_status,
        include_details=include_diagnostics or include_source_details,
    )
    starter_report = starter_status_report(session, today_eastern())
    settings = get_settings()
    trade_policy = dict(last_prediction["trade_policy"] if last_prediction and last_prediction["trade_policy"] else {})
    trade_policy.update(
        {
            "paper_full_game_spread_trading_enabled": settings.paper_full_game_spread_trading_enabled,
            "full_game_spread_audit_gate_enabled": True,
            "full_game_spread_requires_trusted_audit": True,
            "full_game_spread_latest_audit": _latest_spread_audit_counts(session, active_epoch),
            "paper_risk_governance_enabled": settings.paper_risk_governance_enabled,
            "paper_risk_governance_policy_version": settings.paper_risk_governance_policy_version,
            "paper_drawdown_halt_enabled": settings.paper_drawdown_halt_enabled,
            "paper_drawdown_halt_threshold_abs": float(settings.paper_drawdown_halt_threshold_abs),
            "paper_drawdown_halt_threshold_pct": float(settings.paper_drawdown_halt_threshold_pct),
        }
    )
    summary.model_status = ModelStatus(
        active_model_version=active_version.version_tag if active_version else None,
        active_parameter_version=active_parameter_version.version_tag if active_parameter_version else None,
        active_calibration_version=active_parameter_version.version_tag if active_parameter_version else None,
        feature_version=active_version.feature_version if active_version else None,
        calibration_status=str(governance_summary.get("calibration_status") or "not_run"),
        last_training_run=governance_summary.get("last_training_run"),
        last_calibration_run=governance_summary.get("last_calibration_run"),
        candidate_count=int(candidate_count),
        resolved_mature_samples=int(governance_summary["clean_resolved_mature_samples"]),
        raw_resolved_mature_samples=int(governance_summary["raw_resolved_mature_samples"]),
        clean_resolved_mature_samples=int(governance_summary["clean_resolved_mature_samples"]),
        pre_clean_excluded_samples=int(governance_summary["pre_clean_excluded_samples"]),
        training_eligible_count=int(training_eligible_count),
        clean_training_eligible_count=int(governance_summary["clean_training_eligible_count"]),
        last_governance_status=str(governance_summary.get("last_governance_status") or "not_run"),
        governance_training_policy=str(governance_summary["governance_training_policy"]),
        clean_training_start_at=str(governance_summary["clean_training_start_at"]),
        clean_training_start_at_et=str(governance_summary["clean_training_start_at_et"]),
        clean_training_start_date_et=str(governance_summary["clean_training_start_date_et"]),
        clean_filter_exclusion_counts=dict(governance_summary["clean_filter_exclusion_counts"]),
        ignored_pre_clean_artifacts=dict(governance_summary["ignored_pre_clean_artifacts"]),
        governance_parameter_registry=_compact_governance_registry(
            governance_summary.get("governance_parameter_registry"),
            include_details=include_governance_registry_details,
        ),
        family_scope_governance={
            "governance_policy_version": governance_summary.get("governance_policy_version"),
            "enabled": bool(governance_summary.get("family_scope_governance_enabled")),
            "unit_count": governance_summary.get("family_scope_unit_count"),
            "units": governance_summary.get("family_scope_units", {}),
            "adapter_error_count": governance_summary.get("adapter_error_count", 0),
            "adapter_error_reason_counts": governance_summary.get("adapter_error_reason_counts", {}),
            "adapter_errors_excluded_from_training": governance_summary.get(
                "adapter_errors_excluded_from_training",
                True,
            ),
        },
        governance_status=str(governance_summary.get("last_governance_status") or "not_run"),
        trade_policy=trade_policy,
        trade_caps_used=trade_caps_used,
        trade_threshold_policy=dict(governance_summary.get("trade_threshold_policy") or {}),
        data_quality_summary={
            "avg": float(avg_data_quality) if avg_data_quality is not None else None,
            "feature_version": active_version.feature_version if active_version else None,
            "starter_hydration": starter_report["summary"],
            **_compact_candidate_data_quality(
                last_prediction_summary,
                include_details=include_candidate_details,
            ),
        },
        feature_completeness=feature_completeness,
        source_statuses=source_statuses,
        critical_module_warnings=critical_warnings,
        lineup_status=_module_status(source_statuses, "lineup"),
        starter_status=_module_status(source_statuses, "starter_identity"),
        weather_status=_module_status(source_statuses, "park_weather"),
        network_sources_enabled=bool(source_status["feature_sync_enable_network_sources"]),
        public_sources_enabled=bool(source_status["public_sources_enabled"]),
        last_feature_sync_status=dict(compact_source_status.get("last_feature_sync_status") or {}),
        source_details=(
            compact_source_status
            if include_diagnostics or include_source_details
            else {}
        ),
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
