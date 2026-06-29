from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    FeatureSnapshot,
    KalshiMarket,
    MarketMapping,
    MlbGame,
    ModelCandidate,
    ModelPredictionOutput,
    ModelPredictionRun,
    PaperTrade,
)
from app.services.contracts import (
    FIRST_FIVE_SPREAD,
    FIRST_FIVE_TOTAL,
    FIRST_FIVE_WINNER,
    FULL_GAME_SPREAD,
    FULL_GAME_TOTAL,
    FULL_GAME_WINNER,
    PAPER_SUPPORTED_MARKET_FAMILIES,
    contract_labels,
    game_team_codes,
    has_trusted_selection,
    market_type_from_ticker,
)
from app.services.features import FEATURE_VERSION, build_feature_snapshot
from app.services.mapping import infer_market_type
from app.services.modeling import (
    MATURE_MODEL_TAG,
    get_or_create_active_parameter_version,
    get_or_create_mature_model_version,
    score_mature_candidate,
)
from app.services.portfolio import calculate_paper_portfolio, create_balance_snapshot, paper_trade_fee
from app.services.paper_epoch import get_or_create_active_paper_epoch
from app.time_utils import classify_time_bucket, ensure_aware_utc, get_dashboard_zone, utc_now

TRADABLE_MARKET_STATUSES = {"active", "open"}
PLAYABLE_GAME_STATUSES = {"pre-game", "preview", "scheduled", "warmup"}
SELECTION_REQUIRED_FAMILIES = {"full_game_winner", "full_game_spread", "first_five_winner", "first_five_spread"}


@dataclass
class TradeIntent:
    candidate: ModelCandidate
    game: MlbGame
    market: KalshiMarket
    price: Decimal
    labels: object
    score: Decimal
    quantity: int = 1
    sizing: dict[str, Decimal | int | str | None] | None = None


@dataclass(frozen=True)
class PriceContext:
    market_price: Decimal | None
    executable_price: Decimal | None
    source: str | None
    updated_at: datetime | None
    staleness_seconds: int | None
    status: str
    side: str


@dataclass(frozen=True)
class SizingContext:
    bankroll_at_entry: Decimal | None
    risk_pct: Decimal | None
    risk_dollars: Decimal | None
    contracts: int
    estimated_cost_per_contract: Decimal | None
    estimated_total_cost: Decimal | None
    one_contract_expected_value: Decimal | None
    sized_expected_value: Decimal | None
    one_contract_fee_estimate: Decimal | None
    total_fee_estimate: Decimal | None
    status: str

    def as_dict(self) -> dict[str, object]:
        return {
            "bankroll_at_entry": float(self.bankroll_at_entry) if self.bankroll_at_entry is not None else None,
            "risk_pct": float(self.risk_pct) if self.risk_pct is not None else None,
            "risk_dollars": float(self.risk_dollars) if self.risk_dollars is not None else None,
            "contracts": self.contracts,
            "estimated_cost_per_contract": (
                float(self.estimated_cost_per_contract) if self.estimated_cost_per_contract is not None else None
            ),
            "estimated_total_cost": float(self.estimated_total_cost) if self.estimated_total_cost is not None else None,
            "one_contract_expected_value": (
                float(self.one_contract_expected_value) if self.one_contract_expected_value is not None else None
            ),
            "sized_expected_value": float(self.sized_expected_value) if self.sized_expected_value is not None else None,
            "one_contract_fee_estimate": (
                float(self.one_contract_fee_estimate) if self.one_contract_fee_estimate is not None else None
            ),
            "total_fee_estimate": float(self.total_fee_estimate) if self.total_fee_estimate is not None else None,
            "status": self.status,
        }


def _candidate_day_bounds(now: datetime, target_date: date | None = None) -> tuple[date, datetime, datetime]:
    dashboard_zone = get_dashboard_zone()
    day = target_date or now.astimezone(dashboard_zone).date()
    day_start = ensure_aware_utc(datetime.combine(day, time.min, tzinfo=dashboard_zone))
    return day, day_start, day_start + timedelta(days=1)


def _eastern_date(value: datetime) -> date:
    return ensure_aware_utc(value).astimezone(get_dashboard_zone()).date()


def _quantized(value: Decimal, places: str = "0.000001") -> Decimal:
    return value.quantize(Decimal(places))


def _is_executable_price(value: Decimal | None) -> bool:
    return value is not None and Decimal("0") < value < Decimal("1")


def _market_price_timestamp(market: KalshiMarket, now: datetime) -> datetime:
    value = market.market_price_updated_at or market.updated_at or now
    return ensure_aware_utc(value)


def _market_yes_price(market: KalshiMarket, now: datetime | None = None) -> Decimal | None:
    return _market_yes_price_context(market, now or utc_now()).executable_price


def _market_yes_price_context(market: KalshiMarket, now: datetime) -> PriceContext:
    return _market_side_price_context(market, "yes", now)


def _market_side_price_context(market: KalshiMarket, side: str, now: datetime) -> PriceContext:
    settings = get_settings()
    normalized_side = side.strip().lower()
    if normalized_side not in {"yes", "no"}:
        raise ValueError(f"Unsupported contract side: {side}")
    updated_at = _market_price_timestamp(market, now)
    staleness = max(0, int((ensure_aware_utc(now) - updated_at).total_seconds()))

    if normalized_side == "yes":
        candidates: tuple[tuple[str, Decimal | None], ...] = (
            ("yes_ask", market.yes_ask),
            ("orderbook_implied_yes_ask", market.implied_yes_ask),
            (
                "orderbook_best_no_bid_inverse",
                (Decimal("1.0000") - market.best_no_bid) if market.best_no_bid is not None else None,
            ),
        )
    else:
        candidates = (
            ("no_ask", market.no_ask),
            ("orderbook_implied_no_ask", market.implied_no_ask),
            (
                "orderbook_best_yes_bid_inverse",
                (Decimal("1.0000") - market.best_yes_bid) if market.best_yes_bid is not None else None,
            ),
        )
    for source, value in candidates:
        if value is None:
            continue
        price = value.quantize(Decimal("0.0001"))
        if not _is_executable_price(price):
            return PriceContext(price, None, source, updated_at, staleness, "non_executable", normalized_side)
        if staleness > settings.paper_max_price_staleness_seconds:
            return PriceContext(price, None, source, updated_at, staleness, "stale", normalized_side)
        return PriceContext(price, price, source, updated_at, staleness, "fresh_executable", normalized_side)

    if normalized_side == "yes" and market.last_price is not None:
        price = market.last_price.quantize(Decimal("0.0001"))
        source = "last_price_fallback"
        if settings.paper_allow_last_price_fallback_for_trade and _is_executable_price(price):
            status = "fresh_executable" if staleness <= settings.paper_max_price_staleness_seconds else "stale"
            executable = price if status == "fresh_executable" else None
            return PriceContext(price, executable, source, updated_at, staleness, status, normalized_side)
        return PriceContext(price, None, source, updated_at, staleness, "non_executable", normalized_side)

    return PriceContext(None, None, None, updated_at, staleness, "missing", normalized_side)


def _round_up(value: Decimal, step: Decimal) -> Decimal:
    if value <= Decimal("0"):
        return Decimal("0.000000")
    return ((value / step).to_integral_value(rounding=ROUND_CEILING) * step).quantize(Decimal("0.000001"))


def _fee_rounding_step() -> Decimal:
    mode = get_settings().kalshi_fee_rounding_mode.strip().lower()
    if mode in {"centicent", "centicent_only", "0.0001"}:
        return Decimal("0.0001")
    return Decimal("0.01")


def _estimate_trade_fee(price: Decimal | None, quantity: int) -> Decimal | None:
    settings = get_settings()
    if not _is_executable_price(price):
        return None
    raw_fee = settings.kalshi_trade_fee_rate * Decimal(quantity) * price * (Decimal("1") - price)
    return _round_up(raw_fee, _fee_rounding_step())


def _expected_values(
    probability: Decimal | None,
    price: Decimal | None,
    quantity: int,
) -> tuple[Decimal | None, Decimal | None, Decimal | None, Decimal | None]:
    if probability is None or not _is_executable_price(price):
        return None, None, None, None
    fee = _estimate_trade_fee(price, quantity)
    if fee is None:
        return None, None, None, None
    qty = Decimal(quantity)
    # Expanded for auditability: q * (P(win) * payout_profit - P(loss) * stake).
    gross = qty * ((probability * (Decimal("1") - price)) - ((Decimal("1") - probability) * price))
    gross = _quantized(gross)
    edge = _quantized(probability - price)
    net = _quantized(gross - fee)
    return gross, fee, net, edge


def _decimal_json(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _scope_for_family(family: str | None) -> str:
    return "first_five" if (family or "").startswith("first_five") else "full_game"


def _paper_quality_threshold() -> Decimal:
    return get_settings().paper_observation_min_data_quality


def _sizing_context(
    *,
    bankroll: Decimal,
    price: Decimal | None,
    one_contract_net_ev: Decimal | None,
) -> SizingContext:
    settings = get_settings()
    if settings.paper_position_sizing_mode.strip().lower() != "fixed_risk":
        quantity = max(settings.default_paper_contracts, 1)
        fee = _estimate_trade_fee(price, quantity)
        one_fee = _estimate_trade_fee(price, 1)
        cost_per_contract = (price + (one_fee or Decimal("0"))).quantize(Decimal("0.000001")) if price else None
        total_cost = (cost_per_contract * Decimal(quantity)).quantize(Decimal("0.01")) if cost_per_contract else None
        sized_ev = (one_contract_net_ev * Decimal(quantity)).quantize(Decimal("0.000001")) if one_contract_net_ev else None
        return SizingContext(
            bankroll_at_entry=bankroll.quantize(Decimal("0.01")),
            risk_pct=None,
            risk_dollars=None,
            contracts=quantity,
            estimated_cost_per_contract=cost_per_contract,
            estimated_total_cost=total_cost,
            one_contract_expected_value=one_contract_net_ev,
            sized_expected_value=sized_ev,
            one_contract_fee_estimate=one_fee,
            total_fee_estimate=fee,
            status="default_contracts",
        )

    if not _is_executable_price(price):
        return SizingContext(None, None, None, 0, None, None, one_contract_net_ev, None, None, None, "missing_price")

    one_fee = _estimate_trade_fee(price, 1)
    if one_fee is None:
        return SizingContext(None, None, None, 0, None, None, one_contract_net_ev, None, None, None, "missing_fee")

    risk_pct = settings.paper_risk_per_trade_pct
    risk_dollars = (bankroll * risk_pct).quantize(Decimal("0.01"))
    cost_per_contract = (price + one_fee).quantize(Decimal("0.000001"))
    raw_contracts = int((risk_dollars / cost_per_contract).to_integral_value(rounding=ROUND_FLOOR))
    if raw_contracts < settings.paper_min_contracts:
        return SizingContext(
            bankroll_at_entry=bankroll.quantize(Decimal("0.01")),
            risk_pct=risk_pct,
            risk_dollars=risk_dollars,
            contracts=0,
            estimated_cost_per_contract=cost_per_contract,
            estimated_total_cost=None,
            one_contract_expected_value=one_contract_net_ev,
            sized_expected_value=None,
            one_contract_fee_estimate=one_fee,
            total_fee_estimate=None,
            status="insufficient_bankroll_or_contract_size",
        )

    contracts = min(raw_contracts, settings.paper_max_contracts_per_trade)
    total_fee = _estimate_trade_fee(price, contracts)
    total_cost = ((price * Decimal(contracts)) + (total_fee or Decimal("0"))).quantize(Decimal("0.01"))
    sized_ev = (one_contract_net_ev * Decimal(contracts)).quantize(Decimal("0.000001")) if one_contract_net_ev else None
    if total_fee is not None and one_fee is not None:
        sized_ev = (
            ((one_contract_net_ev or Decimal("0")) + one_fee) * Decimal(contracts) - total_fee
        ).quantize(Decimal("0.000001"))
    return SizingContext(
        bankroll_at_entry=bankroll.quantize(Decimal("0.01")),
        risk_pct=risk_pct,
        risk_dollars=risk_dollars,
        contracts=contracts,
        estimated_cost_per_contract=cost_per_contract,
        estimated_total_cost=total_cost,
        one_contract_expected_value=one_contract_net_ev,
        sized_expected_value=sized_ev,
        one_contract_fee_estimate=one_fee,
        total_fee_estimate=total_fee,
        status="sized",
    )


def _diagnostics_payload(
    *,
    mapping: MarketMapping,
    game: MlbGame,
    market: KalshiMarket,
    market_type: str,
    target_date: date,
    minutes_to_start: int,
    price_context: PriceContext,
    probability: Decimal | None,
    gross_ev: Decimal | None,
    fee_estimate: Decimal | None,
    net_ev: Decimal | None,
    probability_edge: Decimal | None,
    data_quality: Decimal | None,
    calibration_status: str | None,
    push_probability: Decimal | None,
    open_trade_exists: bool,
) -> dict[str, object]:
    settings = get_settings()
    settlement_status = _settlement_status(mapping, market)
    mapping_ok = mapping.mapping_status != "needs_review" and (mapping.confidence or Decimal("0")) >= Decimal("0.55")
    supported_ok = market_type in PAPER_SUPPORTED_MARKET_FAMILIES
    if market_type != FULL_GAME_WINNER and settlement_status != "paper_supported":
        supported_ok = False
    game_not_started = minutes_to_start > 0 and _eastern_date(game.scheduled_start) == target_date
    market_open = market.status.strip().lower() in TRADABLE_MARKET_STATUSES
    price_ok = price_context.status == "fresh_executable"
    spread_trading_ok = settings.paper_spread_trading_enabled or market_type not in {FULL_GAME_SPREAD, FIRST_FIVE_SPREAD}
    data_quality_ok = data_quality is not None and data_quality >= _paper_quality_threshold()
    push_ok = not (push_probability is not None and push_probability > Decimal("0") and market_type.endswith(("spread", "total")))
    probability_present = probability is not None
    gross_ev_positive = gross_ev is not None and gross_ev > Decimal("0")
    fee_present = fee_estimate is not None
    probability_edge_ok = probability_edge is not None and probability_edge >= settings.paper_min_prob_edge
    net_ev_ok = net_ev is not None and net_ev >= settings.paper_min_net_ev
    calibration_ok = not settings.paper_require_calibrated_for_trade or calibration_status == "calibrated"
    open_position_ok = not open_trade_exists
    selection_code = (mapping.selection_code or market.selection_code or "").upper()
    trusted_tie_selection = market_type == "first_five_winner" and selection_code == "TIE"
    selection_trusted_ok = (
        not settings.safe_execution_posture
        or market_type not in SELECTION_REQUIRED_FAMILIES
        or trusted_tie_selection
        or _has_trusted_candidate_selection(mapping, game, market)
    )
    pre_quality_gates = [
        mapping_ok,
        supported_ok,
        game_not_started,
        market_open,
        selection_trusted_ok,
        spread_trading_ok,
        price_ok,
        push_ok,
        probability_present,
        gross_ev_positive,
        fee_present,
        probability_edge_ok,
        net_ev_ok,
        calibration_ok,
        settings.paper_candidate_engine_enabled,
        open_position_ok,
    ]
    before_quality = all(pre_quality_gates)
    after_quality = before_quality and data_quality_ok
    flags = {
        "gate_mapping_ok": mapping_ok and supported_ok,
        "gate_market_open": market_open,
        "gate_game_not_started": game_not_started,
        "gate_selection_trusted_ok": selection_trusted_ok,
        "gate_spread_trading_enabled": spread_trading_ok,
        "gate_price_fresh_executable": price_ok,
        "gate_data_quality_ok": data_quality_ok,
        "gate_push_ok": push_ok,
        "gate_probability_present": probability_present,
        "gate_gross_ev_positive": gross_ev_positive,
        "gate_fee_present": fee_present,
        "gate_probability_edge_ok": probability_edge_ok,
        "gate_net_ev_ok": net_ev_ok,
        "gate_calibration_ok": calibration_ok,
        "gate_line_selection_ok": True,
        "gate_caps_ok": True,
        "gate_open_position_ok": open_position_ok,
        "gate_final_trade_eligible": after_quality,
        "blocked_by_quality_only": before_quality and not data_quality_ok,
        "would_pass_ev_if_quality_allowed": gross_ev_positive and net_ev_ok,
        "would_pass_edge_if_quality_allowed": probability_edge_ok,
        "ev_edge_pass_but_quality_fail": gross_ev_positive and net_ev_ok and probability_edge_ok and not data_quality_ok,
        "counterfactual_trade_eligible_before_quality": before_quality,
        "counterfactual_trade_eligible_after_quality": after_quality,
        "market_type_supported": supported_ok,
        "contract_side": price_context.side,
        "paper_quality_threshold": float(_paper_quality_threshold()),
    }
    return flags


def _apply_gate_fields(candidate: ModelCandidate, diagnostics: dict[str, object]) -> None:
    candidate.gate_diagnostics = diagnostics
    for key, value in diagnostics.items():
        if key.startswith("gate_") or key in {
            "blocked_by_quality_only",
            "would_pass_ev_if_quality_allowed",
            "would_pass_edge_if_quality_allowed",
            "ev_edge_pass_but_quality_fail",
            "counterfactual_trade_eligible_before_quality",
            "counterfactual_trade_eligible_after_quality",
        }:
            if hasattr(candidate, key):
                setattr(candidate, key, bool(value))


def _update_gate(candidate: ModelCandidate, key: str, value: bool) -> None:
    diagnostics = dict(candidate.gate_diagnostics or {})
    diagnostics[key] = value
    final = all(
        bool(diagnostics.get(flag))
        for flag in (
            "gate_mapping_ok",
            "gate_market_open",
            "gate_game_not_started",
            "gate_selection_trusted_ok",
            "gate_spread_trading_enabled",
            "gate_price_fresh_executable",
            "gate_data_quality_ok",
            "gate_push_ok",
            "gate_probability_present",
            "gate_gross_ev_positive",
            "gate_fee_present",
            "gate_probability_edge_ok",
            "gate_net_ev_ok",
            "gate_calibration_ok",
            "gate_line_selection_ok",
            "gate_caps_ok",
            "gate_open_position_ok",
        )
    )
    diagnostics["gate_final_trade_eligible"] = final
    _apply_gate_fields(candidate, diagnostics)


def _market_classification_text(market: KalshiMarket) -> str:
    return " ".join(
        value or ""
        for value in (
            market.title,
            market.subtitle,
            market.rules,
            market.yes_subtitle,
            market.no_subtitle,
            market.ticker,
            market.event_ticker,
        )
    )


def _candidate_ids_with_trades(session: Session, candidate_ids: list[int], epoch_id: int | None) -> set[int]:
    if not candidate_ids:
        return set()
    query = select(PaperTrade.candidate_id).where(PaperTrade.candidate_id.in_(candidate_ids))
    if epoch_id is not None:
        query = query.where(PaperTrade.paper_trading_epoch_id == epoch_id)
    return {
        candidate_id
        for candidate_id in session.scalars(query)
        if candidate_id is not None
    }


def _open_trade_for_market(
    session: Session,
    market_ticker: str,
    contract_side: str,
    epoch_id: int | None,
) -> PaperTrade | None:
    query = (
        select(PaperTrade)
        .where(PaperTrade.market_ticker == market_ticker)
        .where(PaperTrade.contract_side == contract_side)
        .where(PaperTrade.status == "open")
    )
    if epoch_id is not None:
        query = query.where(PaperTrade.paper_trading_epoch_id == epoch_id)
    return session.scalar(query.order_by(PaperTrade.entry_time.desc(), PaperTrade.id.desc()).limit(1))


def _open_trade_for_ticker(session: Session, market_ticker: str, epoch_id: int | None) -> PaperTrade | None:
    query = (
        select(PaperTrade)
        .where(PaperTrade.market_ticker == market_ticker)
        .where(PaperTrade.status == "open")
    )
    if epoch_id is not None:
        query = query.where(PaperTrade.paper_trading_epoch_id == epoch_id)
    return session.scalar(query.order_by(PaperTrade.entry_time.desc(), PaperTrade.id.desc()).limit(1))


def _has_trusted_candidate_selection(mapping: MarketMapping, game: MlbGame, market: KalshiMarket) -> bool:
    if has_trusted_selection(game, market.ticker):
        return True
    selection = (mapping.selection_code or market.selection_code or "").upper()
    return selection in game_team_codes(game)


def _settlement_status(mapping: MarketMapping, market: KalshiMarket) -> str | None:
    return mapping.settlement_rule_status or market.settlement_rule_status


def _base_decision(
    mapping: MarketMapping,
    game: MlbGame,
    market: KalshiMarket,
    market_type: str,
    target_date: date,
    minutes_to_start: int,
    price_context: PriceContext,
    probability: Decimal | None,
    gross_ev: Decimal | None,
    fee_estimate: Decimal | None,
    net_ev: Decimal | None,
    probability_edge: Decimal | None,
    data_quality: Decimal | None,
    calibration_status: str | None,
    push_probability: Decimal | None,
) -> str:
    settings = get_settings()
    if mapping.mapping_status == "needs_review" or (mapping.confidence or Decimal("0")) < Decimal("0.55"):
        return "no_trade_mapping_uncertain"
    if market_type not in PAPER_SUPPORTED_MARKET_FAMILIES:
        return "no_trade_unsupported_family"
    settlement_status = _settlement_status(mapping, market)
    if market_type != FULL_GAME_WINNER and settlement_status != "paper_supported":
        if mapping.line_value is None and market.line_value is None and market_type.endswith(("spread", "total")):
            return "no_trade_missing_line"
        return "no_trade_parse_uncertain"
    if market_type in {FULL_GAME_SPREAD, FIRST_FIVE_SPREAD} and not settings.paper_spread_trading_enabled:
        return "no_trade_spread_trading_disabled"
    if minutes_to_start <= 0:
        return "no_trade_game_started"
    if _eastern_date(game.scheduled_start) != target_date:
        return "no_trade_wrong_target_date"
    if market.status.strip().lower() not in TRADABLE_MARKET_STATUSES:
        return "no_trade_market_closed"
    selection_code = (mapping.selection_code or market.selection_code or "").upper()
    trusted_tie_selection = market_type == "first_five_winner" and selection_code == "TIE"
    if (
        settings.safe_execution_posture
        and market_type in SELECTION_REQUIRED_FAMILIES
        and not trusted_tie_selection
        and not _has_trusted_candidate_selection(mapping, game, market)
    ):
        return "no_trade_untrusted_selection"
    if price_context.status == "missing":
        return "no_trade_missing_price"
    if price_context.status == "stale":
        return "no_trade_stale_price"
    if price_context.status != "fresh_executable":
        return "no_trade_non_executable_price"
    if data_quality is None or data_quality < settings.paper_observation_min_data_quality:
        return "no_trade_low_data_quality"
    if push_probability is not None and push_probability > Decimal("0") and market_type.endswith(("spread", "total")):
        return "no_trade_push_possible"
    if probability is None:
        return "no_trade_missing_probability"
    if gross_ev is None or gross_ev <= Decimal("0"):
        return "no_trade_edge_too_low"
    if fee_estimate is None:
        return "no_trade_missing_fee_estimate"
    if probability_edge is None or probability_edge < settings.paper_min_prob_edge:
        return "no_trade_probability_edge_low"
    if net_ev is None or net_ev < settings.paper_min_net_ev:
        return "no_trade_fee_adjusted_ev_too_low"
    if settings.paper_require_calibrated_for_trade and calibration_status != "calibrated":
        return "no_trade_uncalibrated_probability"
    if not settings.paper_candidate_engine_enabled:
        return "candidate_only"
    if settings.safe_execution_posture:
        return "eligible_for_paper_trade"
    return "candidate_only"


LINE_MARKET_FAMILIES = {
    FULL_GAME_SPREAD,
    FULL_GAME_TOTAL,
    FIRST_FIVE_SPREAD,
    FIRST_FIVE_TOTAL,
}


def _slate_trade_counts(
    session: Session,
    target_date: date,
    start: datetime,
    end: datetime,
    epoch_id: int | None,
) -> tuple[int, dict[int, int], dict[str, int], dict[tuple[int, str], int], set[str]]:
    query = (
        select(PaperTrade, ModelCandidate, MlbGame)
        .outerjoin(ModelCandidate, PaperTrade.candidate_id == ModelCandidate.id)
        .outerjoin(MlbGame, ModelCandidate.mlb_game_id == MlbGame.id)
        .where(
            (ModelCandidate.target_date == target_date)
            | (
                (ModelCandidate.target_date.is_(None))
                & (MlbGame.scheduled_start >= start)
                & (MlbGame.scheduled_start < end)
            )
            | (
                (PaperTrade.entry_time >= start)
                & (PaperTrade.entry_time < end)
                & (
                    (ModelCandidate.id.is_(None))
                    | ((ModelCandidate.target_date.is_(None)) & (MlbGame.id.is_(None)))
                )
            )
        )
    )
    if epoch_id is not None:
        query = query.where(PaperTrade.paper_trading_epoch_id == epoch_id)
    rows = list(
        session.execute(query)
    )
    game_counts: dict[int, int] = {}
    family_counts: dict[str, int] = {}
    game_family_counts: dict[tuple[int, str], int] = {}
    market_tickers: set[str] = set()
    for trade, candidate, _game in rows:
        market_tickers.add(trade.market_ticker)
        if candidate and candidate.mlb_game_id is not None:
            game_counts[candidate.mlb_game_id] = game_counts.get(candidate.mlb_game_id, 0) + 1
        family = trade.market_family or (candidate.market_family if candidate else None) or "unknown"
        family_counts[family] = family_counts.get(family, 0) + 1
        if candidate and candidate.mlb_game_id is not None:
            key = (candidate.mlb_game_id, family)
            game_family_counts[key] = game_family_counts.get(key, 0) + 1
    return len(rows), game_counts, family_counts, game_family_counts, market_tickers


def _open_position_count(session: Session, epoch_id: int | None) -> int:
    query = select(func.count(PaperTrade.id)).where(PaperTrade.status == "open")
    if epoch_id is not None:
        query = query.where(PaperTrade.paper_trading_epoch_id == epoch_id)
    return int(session.scalar(query) or 0)


def _trade_rank_score(candidate: ModelCandidate) -> Decimal:
    ev = candidate.net_expected_value or Decimal("0")
    data_quality = candidate.data_quality or Decimal("0")
    probability = candidate.probability_calibrated or candidate.model_probability or Decimal("0")
    return (ev * Decimal("10")) + data_quality + probability


def _game_family_trade_limit(family: str) -> int:
    settings = get_settings()
    limit = max(settings.paper_max_trades_per_game_family, 1)
    if family == FIRST_FIVE_WINNER:
        return limit if settings.paper_allow_multiple_f5_winner_outcomes else 1
    if family in LINE_MARKET_FAMILIES:
        return limit if settings.paper_allow_multiple_lines_per_game_family else 1
    return 1


def _apply_line_selection(intents: list[TradeIntent]) -> tuple[list[TradeIntent], dict[str, int]]:
    counts = {
        "line_selection_groups_considered": 0,
        "line_selection_candidates_kept": 0,
        "line_selection_candidates_rejected": 0,
    }
    if not intents:
        return [], counts

    grouped: dict[tuple[date | None, int | None, str], list[TradeIntent]] = {}
    for intent in intents:
        family = intent.candidate.market_family or "unknown"
        key = (intent.candidate.target_date, intent.candidate.mlb_game_id, family)
        grouped.setdefault(key, []).append(intent)

    selected: list[TradeIntent] = []
    for group in grouped.values():
        counts["line_selection_groups_considered"] += 1
        ranked = sorted(group, key=lambda item: item.score, reverse=True)
        family = ranked[0].candidate.market_family or "unknown"
        limit = _game_family_trade_limit(family)
        kept = ranked[:limit]
        selected.extend(kept)
        counts["line_selection_candidates_kept"] += len(kept)
        for accepted in kept:
            _update_gate(accepted.candidate, "gate_line_selection_ok", True)
        for rejected in ranked[limit:]:
            rejected.candidate.decision = "no_trade_line_selection_not_best"
            _update_gate(rejected.candidate, "gate_line_selection_ok", False)
            counts["line_selection_candidates_rejected"] += 1

    return selected, counts


def _apply_side_conflict_guard(intents: list[TradeIntent]) -> tuple[list[TradeIntent], dict[str, int]]:
    counts = {"no_trade_conflicting_side_signals": 0}
    grouped: dict[str, list[TradeIntent]] = {}
    for intent in intents:
        grouped.setdefault(intent.market.ticker, []).append(intent)

    selected: list[TradeIntent] = []
    for group in grouped.values():
        sides = {str(intent.candidate.contract_side or "").lower() for intent in group}
        if {"yes", "no"}.issubset(sides):
            for intent in group:
                intent.candidate.decision = "no_trade_conflicting_side_signals"
                _update_gate(intent.candidate, "gate_caps_ok", False)
                counts["no_trade_conflicting_side_signals"] += 1
            continue
        selected.extend(group)
    return selected, counts


def _apply_trade_caps(
    session: Session,
    intents: list[TradeIntent],
    target_date: date,
    day_start: datetime,
    day_end: datetime,
    epoch_id: int | None,
) -> tuple[list[TradeIntent], dict[str, int]]:
    settings = get_settings()
    existing_slate, game_counts, family_counts, game_family_counts, market_tickers = _slate_trade_counts(
        session, target_date, day_start, day_end, epoch_id
    )
    open_positions = _open_position_count(session, epoch_id)
    selected: list[TradeIntent] = []
    cap_counts = {
        "candidate_only_due_to_trade_cap": 0,
        "no_trade_market_family_cap": 0,
        "no_trade_game_cap": 0,
        "no_trade_slate_cap": 0,
        "no_trade_correlated_market_cap": 0,
        "no_trade_game_family_cap": 0,
        "no_trade_open_position_cap": 0,
    }

    for intent in sorted(intents, key=lambda item: item.score, reverse=True):
        candidate = intent.candidate
        family = candidate.market_family or "unknown"
        game_id = candidate.mlb_game_id
        if intent.market.ticker in market_tickers:
            candidate.decision = "no_trade_correlated_market_cap"
            _update_gate(candidate, "gate_caps_ok", False)
        elif existing_slate + len(selected) >= settings.paper_max_trades_per_slate:
            candidate.decision = "no_trade_slate_cap"
            _update_gate(candidate, "gate_caps_ok", False)
        elif open_positions + len(selected) >= settings.paper_max_open_positions:
            candidate.decision = "no_trade_open_position_cap"
            _update_gate(candidate, "gate_open_position_ok", False)
        elif game_id is not None and game_counts.get(game_id, 0) >= settings.paper_max_trades_per_game:
            candidate.decision = "no_trade_game_cap"
            _update_gate(candidate, "gate_caps_ok", False)
        elif family_counts.get(family, 0) >= settings.paper_max_trades_per_market_family:
            candidate.decision = "no_trade_market_family_cap"
            _update_gate(candidate, "gate_caps_ok", False)
        elif (
            game_id is not None
            and game_family_counts.get((game_id, family), 0) >= _game_family_trade_limit(family)
        ):
            candidate.decision = "no_trade_game_family_cap"
            _update_gate(candidate, "gate_caps_ok", False)
        else:
            candidate.decision = "paper_trade"
            _update_gate(candidate, "gate_caps_ok", True)
            _update_gate(candidate, "gate_open_position_ok", True)
            selected.append(intent)
            family_counts[family] = family_counts.get(family, 0) + 1
            if game_id is not None:
                game_counts[game_id] = game_counts.get(game_id, 0) + 1
                game_family_counts[(game_id, family)] = game_family_counts.get((game_id, family), 0) + 1
            market_tickers.add(intent.market.ticker)
            continue
        cap_counts[candidate.decision] = cap_counts.get(candidate.decision, 0) + 1
        session.add(candidate)

    return selected, cap_counts


def _trade_risk_cost(trade: PaperTrade) -> Decimal:
    if trade.estimated_total_cost is not None:
        return Decimal(trade.estimated_total_cost).quantize(Decimal("0.01"))
    return ((trade.entry_price * Decimal(trade.quantity)) + paper_trade_fee(trade)).quantize(Decimal("0.01"))


def _risk_scope(value: str | None) -> str:
    return "first_five" if (value or "").startswith("first_five") else "full_game"


def _risk_family(candidate: ModelCandidate | None, trade: PaperTrade | None = None) -> str:
    return (trade.market_family if trade else None) or (candidate.market_family if candidate else None) or "unknown"


def _daily_and_open_risk_usage(
    session: Session,
    target_date: date,
    day_start: datetime,
    day_end: datetime,
    epoch_id: int | None,
) -> dict[str, object]:
    query = select(PaperTrade, ModelCandidate).outerjoin(ModelCandidate, PaperTrade.candidate_id == ModelCandidate.id)
    if epoch_id is not None:
        query = query.where(PaperTrade.paper_trading_epoch_id == epoch_id)
    daily = Decimal("0.00")
    open_risk = Decimal("0.00")
    family: dict[str, Decimal] = {}
    scope: dict[str, Decimal] = {}
    low_price = Decimal("0.00")
    for trade, candidate in session.execute(query):
        cost = _trade_risk_cost(trade)
        is_target_day_trade = (
            candidate is not None
            and candidate.target_date == target_date
        ) or (day_start <= ensure_aware_utc(trade.entry_time) < day_end)
        if is_target_day_trade:
            daily += cost
        if trade.status == "open":
            open_risk += cost
            family_key = _risk_family(candidate, trade)
            scope_key = trade.inning_scope or (candidate.inning_scope if candidate else None) or _risk_scope(family_key)
            family[family_key] = family.get(family_key, Decimal("0.00")) + cost
            scope[scope_key] = scope.get(scope_key, Decimal("0.00")) + cost
            if trade.entry_price < Decimal("0.2000"):
                low_price += cost
    return {
        "daily": daily,
        "open": open_risk,
        "family": family,
        "scope": scope,
        "low_price": low_price,
    }


def _risk_limit(bankroll: Decimal, pct: Decimal) -> Decimal:
    return (bankroll * pct).quantize(Decimal("0.01"))


def _intent_cost(intent: TradeIntent) -> Decimal:
    value = intent.candidate.estimated_total_cost
    if value is not None:
        return Decimal(value).quantize(Decimal("0.01"))
    return (intent.price * Decimal(intent.quantity)).quantize(Decimal("0.01"))


def _adjust_intent_quantity(intent: TradeIntent, quantity: int) -> None:
    candidate = intent.candidate
    intent.quantity = quantity
    one_fee = candidate.one_contract_fee_estimate or _estimate_trade_fee(intent.price, 1) or Decimal("0")
    total_fee = _estimate_trade_fee(intent.price, quantity) or Decimal("0")
    total_cost = ((intent.price * Decimal(quantity)) + total_fee).quantize(Decimal("0.01"))
    one_ev = candidate.one_contract_expected_value or candidate.net_expected_value or Decimal("0")
    sized_ev = ((one_ev + one_fee) * Decimal(quantity) - total_fee).quantize(Decimal("0.000001"))
    candidate.contracts = quantity
    candidate.estimated_cost_per_contract = (intent.price + one_fee).quantize(Decimal("0.000001"))
    candidate.estimated_total_cost = total_cost
    candidate.sized_expected_value = sized_ev
    candidate.total_fee_estimate = total_fee
    sizing = dict(intent.sizing or {})
    sizing.update(
        {
            "contracts": quantity,
            "estimated_total_cost": float(total_cost),
            "sized_expected_value": float(sized_ev),
            "total_fee_estimate": float(total_fee),
            "adjusted_by_aggregate_risk_cap": True,
        }
    )
    intent.sizing = sizing


def _apply_aggregate_risk_caps(
    session: Session,
    intents: list[TradeIntent],
    *,
    target_date: date,
    day_start: datetime,
    day_end: datetime,
    epoch_id: int | None,
    bankroll: Decimal,
) -> tuple[list[TradeIntent], dict[str, int], dict[str, object]]:
    settings = get_settings()
    usage = _daily_and_open_risk_usage(session, target_date, day_start, day_end, epoch_id)
    daily_used = Decimal(usage["daily"])
    open_used = Decimal(usage["open"])
    family_used: dict[str, Decimal] = dict(usage["family"])  # type: ignore[arg-type]
    scope_used: dict[str, Decimal] = dict(usage["scope"])  # type: ignore[arg-type]
    low_price_used = Decimal(usage["low_price"])
    limits = {
        "daily": _risk_limit(bankroll, settings.paper_max_daily_new_risk_pct),
        "open": _risk_limit(bankroll, settings.paper_max_open_risk_pct),
        "family": _risk_limit(bankroll, settings.paper_max_market_family_risk_pct),
        "scope": _risk_limit(bankroll, settings.paper_max_scope_risk_pct),
        "low_price": _risk_limit(bankroll, settings.paper_max_price_bucket_risk_pct_under_20c),
    }
    cap_counts = {
        "no_trade_daily_risk_cap": 0,
        "no_trade_open_risk_cap": 0,
        "no_trade_family_risk_cap": 0,
        "no_trade_scope_risk_cap": 0,
        "no_trade_low_price_bucket_risk_cap": 0,
        "aggregate_risk_quantity_reduced": 0,
    }
    selected: list[TradeIntent] = []

    for intent in intents:
        candidate = intent.candidate
        family = candidate.market_family or "unknown"
        scope = candidate.inning_scope or _risk_scope(family)
        cost = _intent_cost(intent)
        available = {
            "no_trade_daily_risk_cap": limits["daily"] - daily_used,
            "no_trade_open_risk_cap": limits["open"] - open_used,
            "no_trade_family_risk_cap": limits["family"] - family_used.get(family, Decimal("0.00")),
            "no_trade_scope_risk_cap": limits["scope"] - scope_used.get(scope, Decimal("0.00")),
        }
        if intent.price < Decimal("0.2000"):
            available["no_trade_low_price_bucket_risk_cap"] = limits["low_price"] - low_price_used
        blocking_reason = next((reason for reason, remaining in available.items() if remaining < cost), None)
        if blocking_reason:
            cost_per_contract = candidate.estimated_cost_per_contract or (intent.price + (_estimate_trade_fee(intent.price, 1) or Decimal("0")))
            remaining = min(available.values())
            adjusted_quantity = int((max(remaining, Decimal("0")) / cost_per_contract).to_integral_value(rounding=ROUND_FLOOR))
            adjusted_quantity = min(adjusted_quantity, intent.quantity)
            if adjusted_quantity >= settings.paper_min_contracts:
                _adjust_intent_quantity(intent, adjusted_quantity)
                cost = _intent_cost(intent)
                cap_counts["aggregate_risk_quantity_reduced"] += 1
            else:
                candidate.decision = blocking_reason
                _update_gate(candidate, "gate_caps_ok", False)
                cap_counts[blocking_reason] = cap_counts.get(blocking_reason, 0) + 1
                session.add(candidate)
                continue

        candidate.decision = "paper_trade"
        _update_gate(candidate, "gate_caps_ok", True)
        selected.append(intent)
        daily_used += cost
        open_used += cost
        family_used[family] = family_used.get(family, Decimal("0.00")) + cost
        scope_used[scope] = scope_used.get(scope, Decimal("0.00")) + cost
        if intent.price < Decimal("0.2000"):
            low_price_used += cost

    summary = {
        "daily_risk_used": float(daily_used),
        "daily_risk_max": float(limits["daily"]),
        "open_risk_used": float(open_used),
        "open_risk_max": float(limits["open"]),
        "family_risk_used": {key: float(value) for key, value in family_used.items()},
        "family_risk_max": float(limits["family"]),
        "scope_risk_used": {key: float(value) for key, value in scope_used.items()},
        "scope_risk_max": float(limits["scope"]),
        "low_price_bucket_risk_used": float(low_price_used),
        "low_price_bucket_risk_max": float(limits["low_price"]),
    }
    return selected, cap_counts, summary


def _avg_decimal(values: list[Decimal]) -> float | None:
    if not values:
        return None
    return float(sum(values) / Decimal(len(values)))


def _max_decimal(values: list[Decimal]) -> float | None:
    if not values:
        return None
    return float(max(values))


def _min_decimal(values: list[Decimal]) -> float | None:
    if not values:
        return None
    return float(min(values))


def _decision_breakdowns(candidates: list[ModelCandidate]) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    by_family: dict[str, dict[str, int]] = {}
    by_scope: dict[str, dict[str, int]] = {}
    for candidate in candidates:
        family = candidate.market_family or candidate.market_type or "unknown"
        scope = candidate.inning_scope or _scope_for_family(family)
        decision = candidate.decision or "unknown"
        family_bucket = by_family.setdefault(family, {})
        family_bucket[decision] = family_bucket.get(decision, 0) + 1
        scope_bucket = by_scope.setdefault(scope, {})
        scope_bucket[decision] = scope_bucket.get(decision, 0) + 1
    return by_family, by_scope


def _gate_summary(candidates: list[ModelCandidate]) -> dict[str, object]:
    def count(key: str, expected: bool = True) -> int:
        return sum(1 for candidate in candidates if bool((candidate.gate_diagnostics or {}).get(key)) is expected)

    data_quality_values = [candidate.data_quality for candidate in candidates if candidate.data_quality is not None]
    ev_passing_values = [
        candidate.net_expected_value
        for candidate in candidates
        if candidate.net_expected_value is not None and bool((candidate.gate_diagnostics or {}).get("gate_net_ev_ok"))
    ]
    edge_passing_values = [
        candidate.probability_edge
        for candidate in candidates
        if candidate.probability_edge is not None and bool((candidate.gate_diagnostics or {}).get("gate_probability_edge_ok"))
    ]
    return {
        "trade_eligible_before_quality": count("counterfactual_trade_eligible_before_quality"),
        "trade_eligible_after_quality": count("counterfactual_trade_eligible_after_quality"),
        "blocked_by_quality_only": count("blocked_by_quality_only"),
        "would_pass_ev_if_quality_allowed": count("would_pass_ev_if_quality_allowed"),
        "would_pass_edge_if_quality_allowed": count("would_pass_edge_if_quality_allowed"),
        "ev_edge_pass_but_quality_fail": count("ev_edge_pass_but_quality_fail"),
        "blocked_by_ev": count("gate_net_ev_ok", False),
        "blocked_by_edge": count("gate_probability_edge_ok", False),
        "blocked_by_price": count("gate_price_fresh_executable", False),
        "blocked_by_mapping": count("gate_mapping_ok", False),
        "blocked_by_push": count("gate_push_ok", False),
        "blocked_by_line_selection": count("gate_line_selection_ok", False),
        "blocked_by_caps": count("gate_caps_ok", False),
        "average_data_quality": _avg_decimal(data_quality_values),
        "min_data_quality": _min_decimal(data_quality_values),
        "max_data_quality": _max_decimal(data_quality_values),
        "average_net_ev_among_ev_passing": _avg_decimal(ev_passing_values),
        "average_probability_edge_among_edge_passing": _avg_decimal(edge_passing_values),
    }


def _zero_trade_reason(decision_counts: dict[str, int]) -> str | None:
    if not decision_counts:
        return "no_candidates_evaluated"
    blocked = {reason: count for reason, count in decision_counts.items() if reason != "paper_trade"}
    if not blocked:
        return None
    return max(blocked.items(), key=lambda item: item[1])[0]


def generate_candidates(session: Session, target_date: date | None = None) -> dict[str, object]:
    settings = get_settings()
    now = utc_now()
    day, day_start, day_end = _candidate_day_bounds(now, target_date)
    active_epoch = get_or_create_active_paper_epoch(session)
    created_or_updated = 0
    paper_trades = 0
    model_version = get_or_create_mature_model_version(session)
    parameter_version = get_or_create_active_parameter_version(session)
    prediction_run = ModelPredictionRun(
        paper_trading_epoch_id=active_epoch.id,
        started_at=now,
        target_date=day,
        status="running",
        model_version_tag=MATURE_MODEL_TAG,
        feature_version=FEATURE_VERSION,
        trade_policy={
            "paper_max_trades_per_slate": settings.paper_max_trades_per_slate,
            "paper_max_trades_per_game": settings.paper_max_trades_per_game,
            "paper_max_trades_per_market_family": settings.paper_max_trades_per_market_family,
            "paper_max_trades_per_game_family": settings.paper_max_trades_per_game_family,
            "paper_max_open_positions": settings.paper_max_open_positions,
            "paper_min_net_ev": float(settings.paper_min_net_ev),
            "paper_min_prob_edge": float(settings.paper_min_prob_edge),
            "paper_min_data_quality": float(settings.paper_min_data_quality),
            "paper_observation_min_data_quality": float(settings.paper_observation_min_data_quality),
            "live_min_data_quality": float(settings.live_min_data_quality),
            "paper_require_calibrated_for_trade": settings.paper_require_calibrated_for_trade,
            "paper_max_price_staleness_seconds": settings.paper_max_price_staleness_seconds,
            "paper_allow_last_price_fallback_for_trade": settings.paper_allow_last_price_fallback_for_trade,
            "paper_allow_multiple_lines_per_game_family": settings.paper_allow_multiple_lines_per_game_family,
            "paper_allow_multiple_f5_winner_outcomes": settings.paper_allow_multiple_f5_winner_outcomes,
            "paper_spread_trading_enabled": settings.paper_spread_trading_enabled,
            "side_aware_candidates_enabled": True,
            "aggregate_risk_caps_enabled": True,
            "paper_max_daily_new_risk_pct": float(settings.paper_max_daily_new_risk_pct),
            "paper_max_open_risk_pct": float(settings.paper_max_open_risk_pct),
            "paper_max_market_family_risk_pct": float(settings.paper_max_market_family_risk_pct),
            "paper_max_scope_risk_pct": float(settings.paper_max_scope_risk_pct),
            "paper_max_price_bucket_risk_pct_under_20c": float(settings.paper_max_price_bucket_risk_pct_under_20c),
            "kalshi_trade_fee_rate": float(settings.kalshi_trade_fee_rate),
            "kalshi_fee_estimate_mode": settings.kalshi_fee_estimate_mode,
            "kalshi_fee_rounding_mode": settings.kalshi_fee_rounding_mode,
            "kalshi_assume_taker": settings.kalshi_assume_taker,
            "paper_position_sizing_mode": settings.paper_position_sizing_mode,
            "paper_risk_per_trade_pct": float(settings.paper_risk_per_trade_pct),
            "paper_min_contracts": settings.paper_min_contracts,
            "paper_max_contracts_per_trade": settings.paper_max_contracts_per_trade,
        },
    )
    session.add(prediction_run)
    session.flush()

    mappings = session.execute(
        select(MarketMapping, MlbGame, KalshiMarket)
        .join(MlbGame, MarketMapping.mlb_game_id == MlbGame.id)
        .join(KalshiMarket, MarketMapping.kalshi_market_id == KalshiMarket.id)
        .where(MarketMapping.mapping_status.in_(["candidate", "confirmed", "needs_review"]))
        .where(func.lower(MlbGame.status).in_(PLAYABLE_GAME_STATUSES))
        .where(MlbGame.scheduled_start > now)
        .where(MlbGame.scheduled_start >= day_start)
        .where(MlbGame.scheduled_start < day_end)
    ).all()
    warnings: list[str] = []
    if not mappings:
        warnings.append("no_candidates_missing_mappings: run Kalshi market discovery and mapping sync for this target date.")

    trade_intents: list[TradeIntent] = []
    evaluated_candidates: list[ModelCandidate] = []
    outputs_by_candidate_id: dict[int, ModelPredictionOutput] = {}
    stale_price_count = 0
    non_executable_price_count = 0

    for mapping, game, market in mappings:
        minutes_to_start = int((ensure_aware_utc(game.scheduled_start) - now).total_seconds() / 60)
        bucket = classify_time_bucket(minutes_to_start)
        market_type = (
            mapping.market_type
            or market.market_type
            or market_type_from_ticker(market.ticker, infer_market_type(_market_classification_text(market)))
        )
        base_features = build_feature_snapshot(game, market, mapping, session=session, now=now)
        model_score = score_mature_candidate(
            base_features,
            market_type=market_type,
            settlement_status=_settlement_status(mapping, market),
            parameters=parameter_version.parameters,
            parameter_version_tag=parameter_version.version_tag,
        )
        actual_yes_probability = model_score.probability_calibrated or model_score.probability
        actual_no_probability = (Decimal("1.000000") - actual_yes_probability).quantize(Decimal("0.000001"))
        price_contexts = {"yes": _market_side_price_context(market, "yes", now)}
        no_price_context = _market_side_price_context(market, "no", now)
        if no_price_context.market_price is not None:
            price_contexts["no"] = no_price_context

        for contract_side, price_context in price_contexts.items():
            price = price_context.executable_price
            probability = actual_yes_probability if contract_side == "yes" else actual_no_probability
            probability_raw = (
                model_score.probability_raw
                if contract_side == "yes"
                else (Decimal("1.000000") - model_score.probability_raw).quantize(Decimal("0.000001"))
            )
            fair_value = probability.quantize(Decimal("0.0001"))
            features = {**base_features}
            market_context = dict(features.get("market_context") or {})
            market_context["contract_side"] = contract_side
            market_context["side_probability"] = float(probability)
            market_context["actual_yes_probability"] = float(actual_yes_probability)
            market_context["actual_no_probability"] = float(actual_no_probability)
            market_context["executable_price"] = float(price) if price is not None else None
            market_context["executable_price_source"] = price_context.source
            market_context["market_price"] = float(price_context.market_price) if price_context.market_price is not None else None
            market_context["price_status"] = price_context.status
            market_context["price_staleness_seconds"] = price_context.staleness_seconds
            features["market_context"] = market_context
            labels = contract_labels(
                game=game,
                market=market,
                market_ticker=market.ticker,
                market_type=market_type,
                selection_code=mapping.selection_code or market.selection_code,
                contract_side=contract_side,
            )
            actual_display = labels.actual_contract_display or labels.contract_display
            gross_ev, fee, net_ev, probability_edge = _expected_values(probability, price, 1)
            market_context["fee_estimate"] = float(fee) if fee is not None else None
            if price_context.status == "stale":
                stale_price_count += 1
            elif price_context.status not in {"fresh_executable", "missing"}:
                non_executable_price_count += 1
            decision = _base_decision(
                mapping,
                game,
                market,
                market_type,
                day,
                minutes_to_start,
                price_context,
                probability,
                gross_ev,
                fee,
                net_ev,
                probability_edge,
                model_score.data_quality,
                model_score.calibration_status,
                model_score.push_probability,
            )

            existing_candidates = list(
                session.scalars(
                    select(ModelCandidate)
                    .where(ModelCandidate.mapping_id == mapping.id)
                    .where(ModelCandidate.paper_trading_epoch_id == active_epoch.id)
                    .where(ModelCandidate.time_bucket == bucket)
                    .where(ModelCandidate.target_date == day)
                    .where(ModelCandidate.contract_side == contract_side)
                    .order_by(ModelCandidate.evaluated_at.desc(), ModelCandidate.id.desc())
                )
            )
            traded_candidate_ids = _candidate_ids_with_trades(
                session, [candidate.id for candidate in existing_candidates if candidate.id is not None], active_epoch.id
            )
            existing = next(
                (candidate for candidate in existing_candidates if candidate.id not in traded_candidate_ids),
                None,
            )
            open_trade_for_market = _open_trade_for_market(session, market.ticker, contract_side, active_epoch.id)
            open_trade_for_ticker = _open_trade_for_ticker(session, market.ticker, active_epoch.id)
            opposite_open_trade = (
                open_trade_for_ticker
                if open_trade_for_ticker is not None and open_trade_for_ticker.contract_side != contract_side
                else None
            )
            candidate = existing or ModelCandidate(
                paper_trading_epoch_id=active_epoch.id,
                mapping_id=mapping.id,
                mlb_game_id=game.id,
                kalshi_market_id=market.id,
                evaluated_at=now,
            )
            candidate.paper_trading_epoch_id = active_epoch.id
            if open_trade_for_market is not None and price is not None:
                open_trade_for_market.current_price = price
                open_trade_for_market.market_display = open_trade_for_market.market_display or actual_display
                open_trade_for_market.selection_display = open_trade_for_market.selection_display or labels.selection_display
                open_trade_for_market.matchup_display = open_trade_for_market.matchup_display or labels.matchup_display
                open_trade_for_market.contract_display = open_trade_for_market.contract_display or actual_display
                open_trade_for_market.market_family = open_trade_for_market.market_family or mapping.market_family or market.market_family
                open_trade_for_market.line_value = (
                    open_trade_for_market.line_value
                    if open_trade_for_market.line_value is not None
                    else mapping.line_value if mapping.line_value is not None else market.line_value
                )
                open_trade_for_market.selection_code = (
                    open_trade_for_market.selection_code or mapping.selection_code or market.selection_code
                )
                open_trade_for_market.over_under_side = (
                    open_trade_for_market.over_under_side or mapping.over_under_side or market.over_under_side
                )
                open_trade_for_market.inning_scope = open_trade_for_market.inning_scope or mapping.inning_scope or market.inning_scope
                open_trade_for_market.settlement_rule_status = (
                    open_trade_for_market.settlement_rule_status
                    or mapping.settlement_rule_status
                    or market.settlement_rule_status
                )
                session.add(open_trade_for_market)
            if decision == "eligible_for_paper_trade":
                if traded_candidate_ids or open_trade_for_market is not None:
                    decision = "candidate_only_existing_trade"
                elif opposite_open_trade is not None:
                    decision = "no_trade_opposite_side_open"

            candidate.model_version_id = model_version.id
            candidate.evaluated_at = now
            candidate.features = features
            candidate.probability = probability
            candidate.model_probability = probability
            candidate.probability_raw = probability_raw
            candidate.probability_calibrated = probability
            candidate.fair_value = fair_value
            candidate.market_price = price_context.market_price
            candidate.executable_price = price
            candidate.expected_value = gross_ev
            candidate.fee_estimate = fee
            candidate.net_expected_value = net_ev
            candidate.probability_edge = probability_edge
            candidate.target_date = day
            candidate.executable_price_source = price_context.source
            candidate.market_price_updated_at = price_context.updated_at
            candidate.price_staleness_seconds = price_context.staleness_seconds
            candidate.price_status = price_context.status
            candidate.market_type = market_type
            candidate.time_bucket = bucket
            candidate.time_to_start_minutes = minutes_to_start
            candidate.contract_side = contract_side
            candidate.decision = decision
            candidate.model_version_tag = MATURE_MODEL_TAG
            candidate.feature_version = FEATURE_VERSION
            candidate.training_eligible = model_score.training_eligible
            candidate.training_exclusion_reason = model_score.training_exclusion_reason
            if _eastern_date(game.scheduled_start) != day:
                candidate.training_eligible = False
                candidate.training_exclusion_reason = "target_date_mismatch"
            elif minutes_to_start <= 0:
                candidate.training_eligible = False
                candidate.training_exclusion_reason = "candidate_after_game_start"
            elif price_context.status != "fresh_executable":
                candidate.training_eligible = False
                candidate.training_exclusion_reason = f"price_context_{price_context.status}"
            elif fee is None:
                candidate.training_eligible = False
                candidate.training_exclusion_reason = "missing_fee_estimate"
            elif decision == "no_trade_mapping_uncertain":
                candidate.training_eligible = False
                candidate.training_exclusion_reason = "mapping_uncertain"
            elif decision == "no_trade_spread_trading_disabled":
                candidate.training_eligible = False
                candidate.training_exclusion_reason = "spread_trading_disabled"
            candidate.data_quality = model_score.data_quality
            candidate.calibration_status = model_score.calibration_status
            candidate.scoring_rationale = {
                **model_score.rationale,
                "contract_side": contract_side,
                "side_probability": float(probability),
                "actual_yes_probability": float(actual_yes_probability),
                "actual_no_probability": float(actual_no_probability),
                "actual_contract_display": labels.actual_contract_display,
                "normalized_equivalent_display": labels.normalized_equivalent_display,
            }
            candidate.market_display = actual_display
            candidate.selection_display = labels.selection_display
            candidate.matchup_display = labels.matchup_display
            candidate.contract_display = actual_display
            candidate.market_family = mapping.market_family or market.market_family or market_type
            candidate.line_value = mapping.line_value if mapping.line_value is not None else market.line_value
            candidate.selection_code = mapping.selection_code or market.selection_code
            candidate.over_under_side = mapping.over_under_side or market.over_under_side
            candidate.inning_scope = mapping.inning_scope or market.inning_scope
            candidate.settlement_rule_status = mapping.settlement_rule_status or market.settlement_rule_status
            diagnostics = _diagnostics_payload(
                mapping=mapping,
                game=game,
                market=market,
                market_type=market_type,
                target_date=day,
                minutes_to_start=minutes_to_start,
                price_context=price_context,
                probability=probability,
                gross_ev=gross_ev,
                fee_estimate=fee,
                net_ev=net_ev,
                probability_edge=probability_edge,
                data_quality=model_score.data_quality,
                calibration_status=model_score.calibration_status,
                push_probability=model_score.push_probability,
                open_trade_exists=open_trade_for_market is not None or opposite_open_trade is not None,
            )
            diagnostics["actual_contract_display"] = labels.actual_contract_display
            diagnostics["normalized_equivalent_display"] = labels.normalized_equivalent_display
            if decision in {"candidate_only_existing_trade", "no_trade_opposite_side_open"}:
                diagnostics["gate_open_position_ok"] = False
                diagnostics["gate_final_trade_eligible"] = False
            _apply_gate_fields(candidate, diagnostics)
            session.add(candidate)
            session.flush()
            session.add(
                FeatureSnapshot(
                    candidate_id=candidate.id,
                    captured_at=now,
                    features=features,
                    source=FEATURE_VERSION,
                    feature_version=FEATURE_VERSION,
                    source_statuses=features.get("source_statuses"),
                )
            )
            created_or_updated += 1
            evaluated_candidates.append(candidate)

            output = ModelPredictionOutput(
                paper_trading_epoch_id=active_epoch.id,
                prediction_run_id=prediction_run.id,
                candidate_id=candidate.id,
                market_family=candidate.market_family,
                probability_raw=candidate.probability_raw,
                probability_calibrated=candidate.probability_calibrated,
                fair_value=candidate.fair_value,
                executable_price=candidate.executable_price,
                expected_value_gross=candidate.expected_value,
                fee_estimate=candidate.fee_estimate,
                expected_value_net=candidate.net_expected_value,
                probability_edge=candidate.probability_edge,
                executable_price_source=candidate.executable_price_source,
                price_status=candidate.price_status,
                data_quality=candidate.data_quality,
                calibration_status=candidate.calibration_status,
                decision_reason=decision,
                raw_output={
                    **(candidate.scoring_rationale or {}),
                    "target_date": day.isoformat(),
                    "price_context": {
                        "side": contract_side,
                        "market_price": float(price_context.market_price) if price_context.market_price is not None else None,
                        "executable_price": float(price) if price is not None else None,
                        "source": price_context.source,
                        "updated_at": price_context.updated_at.isoformat() if price_context.updated_at else None,
                        "staleness_seconds": price_context.staleness_seconds,
                        "status": price_context.status,
                    },
                    "fee_context": {
                        "fee_estimate": float(fee) if fee is not None else None,
                        "fee_rate": float(settings.kalshi_trade_fee_rate),
                        "fee_estimate_mode": settings.kalshi_fee_estimate_mode,
                        "fee_rounding_mode": settings.kalshi_fee_rounding_mode,
                        "assume_taker": settings.kalshi_assume_taker,
                        "quantity": 1,
                    },
                    "gate_diagnostics": diagnostics,
                },
            )
            session.add(output)
            outputs_by_candidate_id[candidate.id] = output

            if decision == "eligible_for_paper_trade" and price is not None:
                trade_intents.append(
                    TradeIntent(
                        candidate=candidate,
                        game=game,
                        market=market,
                        price=price,
                        labels=labels,
                        score=_trade_rank_score(candidate),
                    )
                )

    side_guarded_trades, side_conflict_counts = _apply_side_conflict_guard(trade_intents)
    line_selected_trades, line_selection_counts = _apply_line_selection(side_guarded_trades)
    for intent in trade_intents:
        output = outputs_by_candidate_id.get(intent.candidate.id)
        if output is not None:
            output.decision_reason = intent.candidate.decision
            raw = dict(output.raw_output or {})
            raw["gate_diagnostics"] = intent.candidate.gate_diagnostics or {}
            output.raw_output = raw
            session.add(output)
        session.add(intent.candidate)

    selected_trades, cap_counts = _apply_trade_caps(session, line_selected_trades, day, day_start, day_end, active_epoch.id)
    session.flush()
    for intent in line_selected_trades:
        output = outputs_by_candidate_id.get(intent.candidate.id)
        if output is not None:
            output.decision_reason = intent.candidate.decision
            raw = dict(output.raw_output or {})
            raw["gate_diagnostics"] = intent.candidate.gate_diagnostics or {}
            output.raw_output = raw
            session.add(output)

    bankroll_for_sizing = Decimal(calculate_paper_portfolio(session, epoch=active_epoch).portfolio_value)

    sized_selected_trades: list[TradeIntent] = []
    sizing_rejections = 0
    for intent in selected_trades:
        sizing = _sizing_context(
            bankroll=bankroll_for_sizing,
            price=intent.price,
            one_contract_net_ev=intent.candidate.net_expected_value,
        )
        if sizing.contracts < settings.paper_min_contracts:
            intent.candidate.decision = "no_trade_insufficient_bankroll_or_contract_size"
            _update_gate(intent.candidate, "gate_caps_ok", False)
            intent.candidate.bankroll_at_entry = sizing.bankroll_at_entry
            intent.candidate.risk_pct = sizing.risk_pct
            intent.candidate.risk_dollars = sizing.risk_dollars
            intent.candidate.contracts = sizing.contracts
            intent.candidate.estimated_cost_per_contract = sizing.estimated_cost_per_contract
            intent.candidate.estimated_total_cost = sizing.estimated_total_cost
            intent.candidate.one_contract_expected_value = sizing.one_contract_expected_value
            intent.candidate.sized_expected_value = sizing.sized_expected_value
            intent.candidate.one_contract_fee_estimate = sizing.one_contract_fee_estimate
            intent.candidate.total_fee_estimate = sizing.total_fee_estimate
            output = outputs_by_candidate_id.get(intent.candidate.id)
            if output is not None:
                output.decision_reason = intent.candidate.decision
                raw = dict(output.raw_output or {})
                raw["sizing"] = sizing.as_dict()
                raw["gate_diagnostics"] = intent.candidate.gate_diagnostics or {}
                output.raw_output = raw
                session.add(output)
            session.add(intent.candidate)
            sizing_rejections += 1
            continue
        intent.quantity = sizing.contracts
        intent.sizing = sizing.as_dict()
        intent.candidate.bankroll_at_entry = sizing.bankroll_at_entry
        intent.candidate.risk_pct = sizing.risk_pct
        intent.candidate.risk_dollars = sizing.risk_dollars
        intent.candidate.contracts = sizing.contracts
        intent.candidate.estimated_cost_per_contract = sizing.estimated_cost_per_contract
        intent.candidate.estimated_total_cost = sizing.estimated_total_cost
        intent.candidate.one_contract_expected_value = sizing.one_contract_expected_value
        intent.candidate.sized_expected_value = sizing.sized_expected_value
        intent.candidate.one_contract_fee_estimate = sizing.one_contract_fee_estimate
        intent.candidate.total_fee_estimate = sizing.total_fee_estimate
        sized_selected_trades.append(intent)

    risk_selected_trades, risk_cap_counts, risk_cap_summary = _apply_aggregate_risk_caps(
        session,
        sized_selected_trades,
        target_date=day,
        day_start=day_start,
        day_end=day_end,
        epoch_id=active_epoch.id,
        bankroll=bankroll_for_sizing,
    )
    for intent in sized_selected_trades:
        output = outputs_by_candidate_id.get(intent.candidate.id)
        if output is not None:
            output.decision_reason = intent.candidate.decision
            raw = dict(output.raw_output or {})
            raw["sizing"] = {
                **(intent.sizing or {}),
                "contracts": intent.quantity,
            }
            raw["gate_diagnostics"] = intent.candidate.gate_diagnostics or {}
            output.raw_output = raw
            session.add(output)
        session.add(intent.candidate)

    paper_trade_side_counts = {"yes": 0, "no": 0}
    for rank, intent in enumerate(risk_selected_trades, start=1):
        candidate = intent.candidate
        existing_trade = session.scalar(
            select(PaperTrade)
            .where(PaperTrade.candidate_id == candidate.id)
            .where(PaperTrade.paper_trading_epoch_id == active_epoch.id)
        )
        if existing_trade is not None:
            candidate.decision = "candidate_only_existing_trade"
            _update_gate(candidate, "gate_open_position_ok", False)
            session.add(candidate)
            output = outputs_by_candidate_id.get(candidate.id)
            if output is not None:
                output.decision_reason = candidate.decision
                session.add(output)
            continue
        trade = PaperTrade(
            paper_trading_epoch_id=active_epoch.id,
            candidate_id=candidate.id,
            market_ticker=intent.market.ticker,
            contract_side=candidate.contract_side or "yes",
            entry_price=intent.price,
            current_price=intent.price,
            quantity=intent.quantity,
            entry_time=now,
            status="open",
            expected_value=candidate.sized_expected_value or candidate.net_expected_value,
            market_display=intent.labels.actual_contract_display or intent.labels.market_display,
            selection_display=intent.labels.selection_display,
            matchup_display=intent.labels.matchup_display,
            contract_display=intent.labels.actual_contract_display or intent.labels.contract_display,
            market_family=candidate.market_family,
            line_value=candidate.line_value,
            selection_code=candidate.selection_code,
            over_under_side=candidate.over_under_side,
            inning_scope=candidate.inning_scope,
            settlement_rule_status=candidate.settlement_rule_status,
            training_eligible=candidate.training_eligible,
            bankroll_at_entry=candidate.bankroll_at_entry,
            risk_pct=candidate.risk_pct,
            risk_dollars=candidate.risk_dollars,
            estimated_cost_per_contract=candidate.estimated_cost_per_contract,
            estimated_total_cost=candidate.estimated_total_cost,
            one_contract_expected_value=candidate.one_contract_expected_value,
            sized_expected_value=candidate.sized_expected_value,
            one_contract_fee_estimate=candidate.one_contract_fee_estimate,
            total_fee_estimate=candidate.total_fee_estimate,
        )
        session.add(trade)
        output = outputs_by_candidate_id.get(candidate.id)
        if output is not None:
            output.trade_rank = rank
            output.decision_reason = "paper_trade"
            raw = dict(output.raw_output or {})
            raw["sizing"] = {
                **(intent.sizing or {}),
                "contracts": intent.quantity,
            }
            raw["gate_diagnostics"] = candidate.gate_diagnostics or {}
            output.raw_output = raw
            session.add(output)
        paper_trades += 1
        side_key = (candidate.contract_side or "unknown").lower()
        if side_key in paper_trade_side_counts:
            paper_trade_side_counts[side_key] += 1

    session.flush()
    decision_counts: dict[str, int] = {}
    decision_counts_by_side: dict[str, dict[str, int]] = {}
    edge_values_by_side: dict[str, list[Decimal]] = {"yes": [], "no": []}
    net_ev_values_by_side: dict[str, list[Decimal]] = {"yes": [], "no": []}
    for candidate in evaluated_candidates:
        decision = candidate.decision or "unknown"
        decision_counts[decision] = decision_counts.get(decision, 0) + 1
        side = (candidate.contract_side or "unknown").lower()
        side_bucket = decision_counts_by_side.setdefault(side, {})
        side_bucket[decision] = side_bucket.get(decision, 0) + 1
        if side in edge_values_by_side and candidate.probability_edge is not None:
            edge_values_by_side[side].append(candidate.probability_edge)
        if side in net_ev_values_by_side and candidate.net_expected_value is not None:
            net_ev_values_by_side[side].append(candidate.net_expected_value)
    decision_breakdown_by_family, decision_breakdown_by_scope = _decision_breakdowns(evaluated_candidates)
    gate_summary = _gate_summary(evaluated_candidates)
    edge_values = [candidate.probability_edge for candidate in evaluated_candidates if candidate.probability_edge is not None]
    net_ev_values = [
        candidate.net_expected_value for candidate in evaluated_candidates if candidate.net_expected_value is not None
    ]
    fee_values = [candidate.fee_estimate for candidate in evaluated_candidates if candidate.fee_estimate is not None]
    edge_or_fee_reasons = {
        "no_trade_edge_too_low",
        "no_trade_fee_adjusted_ev_too_low",
        "no_trade_probability_edge_low",
        "no_trade_missing_fee_estimate",
    }
    trades_blocked_by_edge_or_fee = sum(decision_counts.get(reason, 0) for reason in edge_or_fee_reasons)
    all_cap_counts = {**cap_counts}
    for key, value in side_conflict_counts.items():
        all_cap_counts[key] = all_cap_counts.get(key, 0) + value
    for key, value in risk_cap_counts.items():
        all_cap_counts[key] = all_cap_counts.get(key, 0) + value
    trades_blocked_by_caps = sum(
        value for key, value in all_cap_counts.items() if key != "aggregate_risk_quantity_reduced"
    ) + sizing_rejections

    snapshot = create_balance_snapshot(session, source="candidate_engine")
    prediction_run.completed_at = now
    prediction_run.status = "completed"
    prediction_run.candidates_evaluated = created_or_updated
    prediction_run.trades_created = paper_trades
    prediction_run.summary = {
        "decision_counts": decision_counts,
        "decision_counts_by_side": decision_counts_by_side,
        "candidate_diagnostics": gate_summary,
        "by_family": decision_breakdown_by_family,
        "by_scope": decision_breakdown_by_scope,
        "cap_counts": all_cap_counts,
        "risk_caps": risk_cap_summary,
        "spread_trading_enabled": settings.paper_spread_trading_enabled,
        "side_aware_candidates_enabled": True,
        "risk_caps_enabled": True,
        "candidates_yes": len([candidate for candidate in evaluated_candidates if candidate.contract_side == "yes"]),
        "candidates_no": len([candidate for candidate in evaluated_candidates if candidate.contract_side == "no"]),
        "paper_trades_yes": paper_trade_side_counts["yes"],
        "paper_trades_no": paper_trade_side_counts["no"],
        "avg_net_ev_by_side": {
            "yes": _avg_decimal(net_ev_values_by_side["yes"]),
            "no": _avg_decimal(net_ev_values_by_side["no"]),
        },
        "avg_probability_edge_by_side": {
            "yes": _avg_decimal(edge_values_by_side["yes"]),
            "no": _avg_decimal(edge_values_by_side["no"]),
        },
        "actual_contract_parse_failures": 0,
        "side_aware_verified_count": created_or_updated,
        "line_selection": line_selection_counts,
        "warnings": warnings,
        "eligible_trade_intents": len(trade_intents),
        "trade_eligible_after_side_conflict_guard": len(side_guarded_trades),
        "trade_eligible_after_line_selection": len(line_selected_trades),
        "trade_eligible_before_caps": len(line_selected_trades),
        "trades_blocked_by_edge_or_fee": trades_blocked_by_edge_or_fee,
        "trades_blocked_by_line_selection": line_selection_counts["line_selection_candidates_rejected"],
        "stale_price_count": stale_price_count,
        "paper_trades": paper_trades,
        "sizing_rejections": sizing_rejections,
    }
    session.add(prediction_run)
    session.commit()
    zero_trade_reason = (
        "no_candidates_missing_mappings"
        if not mappings and paper_trades == 0
        else _zero_trade_reason(decision_counts) if paper_trades == 0 else None
    )
    return {
        "date": int(day.strftime("%Y%m%d")),
        "target_date": day.isoformat(),
        "current_eastern_date": _eastern_date(now).isoformat(),
        "candidates": created_or_updated,
        "candidates_evaluated": created_or_updated,
        "candidate_only_count": sum(count for reason, count in decision_counts.items() if reason.startswith("candidate_only")),
        "evaluated_game_count": len({game.id for _mapping, game, _market in mappings}),
        "mappings_considered": len(mappings),
        "paper_trades": paper_trades,
        "trades_created": paper_trades,
        "model_version": model_version.version_tag,
        "parameter_version": parameter_version.version_tag,
        "feature_version": FEATURE_VERSION,
        "prediction_run_id": prediction_run.id,
        "prediction_run_target_date": prediction_run.target_date.isoformat() if prediction_run.target_date else None,
        "snapshot_id": snapshot.id,
        "decision_counts": decision_counts,
        "decision_counts_by_side": decision_counts_by_side,
        "candidate_diagnostics": gate_summary,
        "decision_breakdown_by_family": decision_breakdown_by_family,
        "decision_breakdown_by_scope": decision_breakdown_by_scope,
        "cap_counts": all_cap_counts,
        "risk_caps": risk_cap_summary,
        "spread_trading_enabled": settings.paper_spread_trading_enabled,
        "side_aware_candidates_enabled": True,
        "risk_caps_enabled": True,
        "candidates_yes": len([candidate for candidate in evaluated_candidates if candidate.contract_side == "yes"]),
        "candidates_no": len([candidate for candidate in evaluated_candidates if candidate.contract_side == "no"]),
        "paper_trades_yes": paper_trade_side_counts["yes"],
        "paper_trades_no": paper_trade_side_counts["no"],
        "avg_net_ev_by_side": {
            "yes": _avg_decimal(net_ev_values_by_side["yes"]),
            "no": _avg_decimal(net_ev_values_by_side["no"]),
        },
        "avg_probability_edge_by_side": {
            "yes": _avg_decimal(edge_values_by_side["yes"]),
            "no": _avg_decimal(edge_values_by_side["no"]),
        },
        "actual_contract_parse_failures": 0,
        "side_aware_verified_count": created_or_updated,
        "trade_eligible_before_quality": gate_summary["trade_eligible_before_quality"],
        "trade_eligible_after_quality": gate_summary["trade_eligible_after_quality"],
        "blocked_by_quality_only": gate_summary["blocked_by_quality_only"],
        "would_pass_ev_if_quality_allowed": gate_summary["would_pass_ev_if_quality_allowed"],
        "would_pass_edge_if_quality_allowed": gate_summary["would_pass_edge_if_quality_allowed"],
        "ev_edge_pass_but_quality_fail": gate_summary["ev_edge_pass_but_quality_fail"],
        "blocked_by_ev": gate_summary["blocked_by_ev"],
        "blocked_by_edge": gate_summary["blocked_by_edge"],
        "blocked_by_price": gate_summary["blocked_by_price"],
        "blocked_by_mapping": gate_summary["blocked_by_mapping"],
        "blocked_by_push": gate_summary["blocked_by_push"],
        "blocked_by_line_selection": gate_summary["blocked_by_line_selection"],
        "blocked_by_caps": gate_summary["blocked_by_caps"],
        "trade_eligible_after_side_conflict_guard": len(side_guarded_trades),
        "trade_eligible_before_caps": len(line_selected_trades),
        "trade_eligible_after_ev_filters": len(trade_intents),
        "trade_eligible_after_line_selection": len(line_selected_trades),
        "trades_blocked_by_caps": trades_blocked_by_caps,
        "trades_blocked_by_edge_or_fee": trades_blocked_by_edge_or_fee,
        "trades_blocked_by_line_selection": line_selection_counts["line_selection_candidates_rejected"],
        "trades_blocked_by_line_selection_or_correlation": line_selection_counts["line_selection_candidates_rejected"]
        + all_cap_counts.get("no_trade_correlated_market_cap", 0),
        "stale_price_count": stale_price_count,
        "non_executable_price_count": non_executable_price_count,
        "line_selection_groups_considered": line_selection_counts["line_selection_groups_considered"],
        "line_selection_candidates_kept": line_selection_counts["line_selection_candidates_kept"],
        "line_selection_candidates_rejected": line_selection_counts["line_selection_candidates_rejected"],
        "avg_probability_edge": _avg_decimal(edge_values),
        "avg_expected_value_net": _avg_decimal(net_ev_values),
        "max_expected_value_net": _max_decimal(net_ev_values),
        "average_data_quality": gate_summary["average_data_quality"],
        "min_data_quality": gate_summary["min_data_quality"],
        "max_data_quality": gate_summary["max_data_quality"],
        "average_net_ev_among_ev_passing": gate_summary["average_net_ev_among_ev_passing"],
        "average_probability_edge_among_edge_passing": gate_summary["average_probability_edge_among_edge_passing"],
        "fee_estimate_avg": _avg_decimal(fee_values),
        "zero_trade_reason": zero_trade_reason,
        "warnings": warnings,
        "trade_policy": prediction_run.trade_policy,
        "active_epoch": {
            "id": active_epoch.id,
            "epoch_key": active_epoch.epoch_key,
            "display_name": active_epoch.display_name,
            "starting_balance": float(active_epoch.starting_balance),
        },
    }
