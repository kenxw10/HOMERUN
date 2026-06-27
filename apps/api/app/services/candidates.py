from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_CEILING

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
    FULL_GAME_WINNER,
    PAPER_SUPPORTED_MARKET_FAMILIES,
    contract_labels,
    game_team_codes,
    has_trusted_selection,
    market_type_from_ticker,
)
from app.services.features import FEATURE_VERSION, build_feature_snapshot
from app.services.mapping import infer_market_type
from app.services.modeling import MATURE_MODEL_TAG, get_or_create_mature_model_version, score_mature_candidate
from app.services.portfolio import create_balance_snapshot
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


@dataclass(frozen=True)
class PriceContext:
    market_price: Decimal | None
    executable_price: Decimal | None
    source: str | None
    updated_at: datetime | None
    staleness_seconds: int | None
    status: str


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
    value = market.updated_at or now
    return ensure_aware_utc(value)


def _market_yes_price(market: KalshiMarket, now: datetime | None = None) -> Decimal | None:
    return _market_yes_price_context(market, now or utc_now()).executable_price


def _market_yes_price_context(market: KalshiMarket, now: datetime) -> PriceContext:
    settings = get_settings()
    updated_at = _market_price_timestamp(market, now)
    staleness = max(0, int((ensure_aware_utc(now) - updated_at).total_seconds()))

    candidates: tuple[tuple[str, Decimal | None], ...] = (
        ("yes_ask", market.yes_ask),
        ("orderbook_implied_yes_ask", market.implied_yes_ask),
        ("orderbook_best_no_bid_inverse", (Decimal("1.0000") - market.best_no_bid) if market.best_no_bid is not None else None),
    )
    for source, value in candidates:
        if value is None:
            continue
        price = value.quantize(Decimal("0.0001"))
        if not _is_executable_price(price):
            return PriceContext(price, None, source, updated_at, staleness, "non_executable")
        if staleness > settings.paper_max_price_staleness_seconds:
            return PriceContext(price, None, source, updated_at, staleness, "stale")
        return PriceContext(price, price, source, updated_at, staleness, "fresh_executable")

    if market.last_price is not None:
        price = market.last_price.quantize(Decimal("0.0001"))
        source = "last_price_fallback"
        if settings.paper_allow_last_price_fallback_for_trade and _is_executable_price(price):
            status = "fresh_executable" if staleness <= settings.paper_max_price_staleness_seconds else "stale"
            executable = price if status == "fresh_executable" else None
            return PriceContext(price, executable, source, updated_at, staleness, status)
        return PriceContext(price, None, source, updated_at, staleness, "non_executable")

    return PriceContext(None, None, None, updated_at, staleness, "missing")


def _round_up(value: Decimal, step: Decimal) -> Decimal:
    if value <= Decimal("0"):
        return Decimal("0.000000")
    return ((value / step).to_integral_value(rounding=ROUND_CEILING) * step).quantize(Decimal("0.000001"))


def _fee_rounding_step() -> Decimal:
    mode = get_settings().kalshi_fee_rounding_mode.strip().lower()
    if mode == "cent":
        return Decimal("0.01")
    return Decimal("0.0001")


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


def _candidate_ids_with_trades(session: Session, candidate_ids: list[int]) -> set[int]:
    if not candidate_ids:
        return set()
    return {
        candidate_id
        for candidate_id in session.scalars(
            select(PaperTrade.candidate_id).where(PaperTrade.candidate_id.in_(candidate_ids))
        )
        if candidate_id is not None
    }


def _open_trade_for_market(session: Session, market_ticker: str, contract_side: str) -> PaperTrade | None:
    return session.scalar(
        select(PaperTrade)
        .where(PaperTrade.market_ticker == market_ticker)
        .where(PaperTrade.contract_side == contract_side)
        .where(PaperTrade.status == "open")
        .order_by(PaperTrade.entry_time.desc(), PaperTrade.id.desc())
        .limit(1)
    )


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
    if data_quality is None or data_quality < settings.paper_min_data_quality:
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


def _slate_trade_counts(
    session: Session,
    target_date: date,
    start: datetime,
    end: datetime,
) -> tuple[int, dict[int, int], dict[str, int], set[tuple[int, str]], set[str]]:
    rows = list(
        session.execute(
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
            )
        )
    )
    game_counts: dict[int, int] = {}
    family_counts: dict[str, int] = {}
    game_family_pairs: set[tuple[int, str]] = set()
    market_tickers: set[str] = set()
    for trade, candidate, _game in rows:
        market_tickers.add(trade.market_ticker)
        if candidate and candidate.mlb_game_id is not None:
            game_counts[candidate.mlb_game_id] = game_counts.get(candidate.mlb_game_id, 0) + 1
        family = trade.market_family or (candidate.market_family if candidate else None) or "unknown"
        family_counts[family] = family_counts.get(family, 0) + 1
        if candidate and candidate.mlb_game_id is not None:
            game_family_pairs.add((candidate.mlb_game_id, family))
    return len(rows), game_counts, family_counts, game_family_pairs, market_tickers


def _open_position_count(session: Session) -> int:
    return int(session.scalar(select(func.count(PaperTrade.id)).where(PaperTrade.status == "open")) or 0)


def _trade_rank_score(candidate: ModelCandidate) -> Decimal:
    ev = candidate.net_expected_value or Decimal("0")
    data_quality = candidate.data_quality or Decimal("0")
    probability = candidate.probability_calibrated or candidate.model_probability or Decimal("0")
    return (ev * Decimal("10")) + data_quality + probability


def _apply_line_selection(intents: list[TradeIntent]) -> tuple[list[TradeIntent], dict[str, int]]:
    settings = get_settings()
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
        limit = max(settings.paper_max_trades_per_game_family, 1)
        if not settings.paper_allow_multiple_lines_per_game_family:
            limit = 1
        if family == "first_five_winner" and not settings.paper_allow_multiple_f5_winner_outcomes:
            limit = 1
        kept = ranked[:limit]
        selected.extend(kept)
        counts["line_selection_candidates_kept"] += len(kept)
        for rejected in ranked[limit:]:
            rejected.candidate.decision = "no_trade_line_selection_not_best"
            counts["line_selection_candidates_rejected"] += 1

    return selected, counts


def _apply_trade_caps(
    session: Session,
    intents: list[TradeIntent],
    target_date: date,
    day_start: datetime,
    day_end: datetime,
) -> tuple[list[TradeIntent], dict[str, int]]:
    settings = get_settings()
    existing_slate, game_counts, family_counts, game_family_pairs, market_tickers = _slate_trade_counts(
        session, target_date, day_start, day_end
    )
    open_positions = _open_position_count(session)
    selected: list[TradeIntent] = []
    cap_counts = {
        "candidate_only_due_to_trade_cap": 0,
        "no_trade_market_family_cap": 0,
        "no_trade_game_cap": 0,
        "no_trade_slate_cap": 0,
        "no_trade_correlated_market_cap": 0,
        "no_trade_open_position_cap": 0,
    }

    for intent in sorted(intents, key=lambda item: item.score, reverse=True):
        candidate = intent.candidate
        family = candidate.market_family or "unknown"
        game_id = candidate.mlb_game_id
        if intent.market.ticker in market_tickers:
            candidate.decision = "no_trade_correlated_market_cap"
        elif existing_slate + len(selected) >= settings.paper_max_trades_per_slate:
            candidate.decision = "no_trade_slate_cap"
        elif open_positions + len(selected) >= settings.paper_max_open_positions:
            candidate.decision = "no_trade_open_position_cap"
        elif game_id is not None and game_counts.get(game_id, 0) >= settings.paper_max_trades_per_game:
            candidate.decision = "no_trade_game_cap"
        elif family_counts.get(family, 0) >= settings.paper_max_trades_per_market_family:
            candidate.decision = "no_trade_market_family_cap"
        elif game_id is not None and (game_id, family) in game_family_pairs:
            candidate.decision = "no_trade_correlated_market_cap"
        else:
            candidate.decision = "paper_trade"
            selected.append(intent)
            family_counts[family] = family_counts.get(family, 0) + 1
            if game_id is not None:
                game_counts[game_id] = game_counts.get(game_id, 0) + 1
                game_family_pairs.add((game_id, family))
            market_tickers.add(intent.market.ticker)
            continue
        cap_counts[candidate.decision] = cap_counts.get(candidate.decision, 0) + 1
        session.add(candidate)

    return selected, cap_counts


def _avg_decimal(values: list[Decimal]) -> float | None:
    if not values:
        return None
    return float(sum(values) / Decimal(len(values)))


def _max_decimal(values: list[Decimal]) -> float | None:
    if not values:
        return None
    return float(max(values))


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
    created_or_updated = 0
    paper_trades = 0
    model_version = get_or_create_mature_model_version(session)
    prediction_run = ModelPredictionRun(
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
            "paper_require_calibrated_for_trade": settings.paper_require_calibrated_for_trade,
            "paper_max_price_staleness_seconds": settings.paper_max_price_staleness_seconds,
            "paper_allow_last_price_fallback_for_trade": settings.paper_allow_last_price_fallback_for_trade,
            "paper_allow_multiple_lines_per_game_family": settings.paper_allow_multiple_lines_per_game_family,
            "paper_allow_multiple_f5_winner_outcomes": settings.paper_allow_multiple_f5_winner_outcomes,
            "kalshi_trade_fee_rate": float(settings.kalshi_trade_fee_rate),
            "kalshi_fee_estimate_mode": settings.kalshi_fee_estimate_mode,
            "kalshi_fee_rounding_mode": settings.kalshi_fee_rounding_mode,
            "kalshi_assume_taker": settings.kalshi_assume_taker,
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
        price_context = _market_yes_price_context(market, now)
        price = price_context.executable_price
        contract_side = "yes"
        features = build_feature_snapshot(game, market, mapping, session=session, now=now)
        labels = contract_labels(
            game=game,
            market=market,
            market_ticker=market.ticker,
            market_type=market_type,
            selection_code=mapping.selection_code or market.selection_code,
        )
        model_score = score_mature_candidate(
            features,
            market_type=market_type,
            settlement_status=_settlement_status(mapping, market),
        )
        probability = model_score.probability_calibrated or model_score.probability
        fair_value = model_score.fair_value
        gross_ev, fee, net_ev, probability_edge = _expected_values(
            probability, price, settings.default_paper_contracts
        )
        if price_context.status == "stale":
            stale_price_count += 1
        elif price_context.status not in {"fresh_executable", "missing"}:
            non_executable_price_count += 1
        market_context = features.get("market_context")
        if isinstance(market_context, dict):
            market_context["executable_price"] = float(price) if price is not None else None
            market_context["executable_price_source"] = price_context.source
            market_context["market_price"] = float(price_context.market_price) if price_context.market_price is not None else None
            market_context["price_status"] = price_context.status
            market_context["price_staleness_seconds"] = price_context.staleness_seconds
            market_context["fee_estimate"] = float(fee) if fee is not None else None
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
                .where(ModelCandidate.time_bucket == bucket)
                .where(ModelCandidate.target_date == day)
                .order_by(ModelCandidate.evaluated_at.desc(), ModelCandidate.id.desc())
            )
        )
        traded_candidate_ids = _candidate_ids_with_trades(
            session, [candidate.id for candidate in existing_candidates if candidate.id is not None]
        )
        existing = next(
            (candidate for candidate in existing_candidates if candidate.id not in traded_candidate_ids),
            None,
        )
        open_trade_for_market = _open_trade_for_market(session, market.ticker, contract_side)
        candidate = existing or ModelCandidate(
            mapping_id=mapping.id,
            mlb_game_id=game.id,
            kalshi_market_id=market.id,
            evaluated_at=now,
        )
        if open_trade_for_market is not None and price is not None:
            open_trade_for_market.current_price = price
            open_trade_for_market.market_display = open_trade_for_market.market_display or labels.market_display
            open_trade_for_market.selection_display = open_trade_for_market.selection_display or labels.selection_display
            open_trade_for_market.matchup_display = open_trade_for_market.matchup_display or labels.matchup_display
            open_trade_for_market.contract_display = open_trade_for_market.contract_display or labels.contract_display
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
        if (traded_candidate_ids or open_trade_for_market is not None) and decision == "eligible_for_paper_trade":
            decision = "candidate_only_existing_trade"

        candidate.model_version_id = model_version.id
        candidate.evaluated_at = now
        candidate.features = features
        candidate.probability = probability
        candidate.model_probability = probability
        candidate.probability_raw = model_score.probability_raw
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
        candidate.data_quality = model_score.data_quality
        candidate.calibration_status = model_score.calibration_status
        candidate.scoring_rationale = model_score.rationale
        candidate.market_display = labels.market_display
        candidate.selection_display = labels.selection_display
        candidate.matchup_display = labels.matchup_display
        candidate.contract_display = labels.contract_display
        candidate.market_family = mapping.market_family or market.market_family or market_type
        candidate.line_value = mapping.line_value if mapping.line_value is not None else market.line_value
        candidate.selection_code = mapping.selection_code or market.selection_code
        candidate.over_under_side = mapping.over_under_side or market.over_under_side
        candidate.inning_scope = mapping.inning_scope or market.inning_scope
        candidate.settlement_rule_status = mapping.settlement_rule_status or market.settlement_rule_status
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
                **model_score.rationale,
                "target_date": day.isoformat(),
                "price_context": {
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
                    "quantity": settings.default_paper_contracts,
                },
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

    line_selected_trades, line_selection_counts = _apply_line_selection(trade_intents)
    for intent in trade_intents:
        output = outputs_by_candidate_id.get(intent.candidate.id)
        if output is not None:
            output.decision_reason = intent.candidate.decision
            session.add(output)
        session.add(intent.candidate)

    selected_trades, cap_counts = _apply_trade_caps(session, line_selected_trades, day, day_start, day_end)
    session.flush()
    for intent in line_selected_trades:
        output = outputs_by_candidate_id.get(intent.candidate.id)
        if output is not None:
            output.decision_reason = intent.candidate.decision
            session.add(output)

    for rank, intent in enumerate(selected_trades, start=1):
        candidate = intent.candidate
        existing_trade = session.scalar(select(PaperTrade).where(PaperTrade.candidate_id == candidate.id))
        if existing_trade is not None:
            candidate.decision = "candidate_only_existing_trade"
            session.add(candidate)
            output = outputs_by_candidate_id.get(candidate.id)
            if output is not None:
                output.decision_reason = candidate.decision
                session.add(output)
            continue
        trade = PaperTrade(
            candidate_id=candidate.id,
            market_ticker=intent.market.ticker,
            contract_side="yes",
            entry_price=intent.price,
            current_price=intent.price,
            quantity=settings.default_paper_contracts,
            entry_time=now,
            status="open",
            expected_value=candidate.net_expected_value,
            market_display=intent.labels.market_display,
            selection_display=intent.labels.selection_display,
            matchup_display=intent.labels.matchup_display,
            contract_display=intent.labels.contract_display,
            market_family=candidate.market_family,
            line_value=candidate.line_value,
            selection_code=candidate.selection_code,
            over_under_side=candidate.over_under_side,
            inning_scope=candidate.inning_scope,
            settlement_rule_status=candidate.settlement_rule_status,
            training_eligible=candidate.training_eligible,
        )
        session.add(trade)
        output = outputs_by_candidate_id.get(candidate.id)
        if output is not None:
            output.trade_rank = rank
            output.decision_reason = "paper_trade"
            session.add(output)
        paper_trades += 1

    session.flush()
    decision_counts: dict[str, int] = {}
    for candidate in evaluated_candidates:
        decision = candidate.decision or "unknown"
        decision_counts[decision] = decision_counts.get(decision, 0) + 1
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
    trades_blocked_by_caps = sum(cap_counts.values())

    snapshot = create_balance_snapshot(session, source="candidate_engine")
    prediction_run.completed_at = now
    prediction_run.status = "completed"
    prediction_run.candidates_evaluated = created_or_updated
    prediction_run.trades_created = paper_trades
    prediction_run.summary = {
        "decision_counts": decision_counts,
        "cap_counts": cap_counts,
        "line_selection": line_selection_counts,
        "eligible_trade_intents": len(trade_intents),
        "trade_eligible_after_line_selection": len(line_selected_trades),
        "trade_eligible_before_caps": len(line_selected_trades),
        "trades_blocked_by_edge_or_fee": trades_blocked_by_edge_or_fee,
        "trades_blocked_by_line_selection": line_selection_counts["line_selection_candidates_rejected"],
        "stale_price_count": stale_price_count,
        "paper_trades": paper_trades,
    }
    session.add(prediction_run)
    session.commit()
    zero_trade_reason = _zero_trade_reason(decision_counts) if paper_trades == 0 else None
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
        "feature_version": FEATURE_VERSION,
        "prediction_run_id": prediction_run.id,
        "prediction_run_target_date": prediction_run.target_date.isoformat() if prediction_run.target_date else None,
        "snapshot_id": snapshot.id,
        "decision_counts": decision_counts,
        "cap_counts": cap_counts,
        "trade_eligible_before_caps": len(line_selected_trades),
        "trade_eligible_after_ev_filters": len(trade_intents),
        "trade_eligible_after_line_selection": len(line_selected_trades),
        "trades_blocked_by_caps": trades_blocked_by_caps,
        "trades_blocked_by_edge_or_fee": trades_blocked_by_edge_or_fee,
        "trades_blocked_by_line_selection": line_selection_counts["line_selection_candidates_rejected"],
        "trades_blocked_by_line_selection_or_correlation": line_selection_counts["line_selection_candidates_rejected"]
        + cap_counts.get("no_trade_correlated_market_cap", 0),
        "stale_price_count": stale_price_count,
        "non_executable_price_count": non_executable_price_count,
        "line_selection_groups_considered": line_selection_counts["line_selection_groups_considered"],
        "line_selection_candidates_kept": line_selection_counts["line_selection_candidates_kept"],
        "line_selection_candidates_rejected": line_selection_counts["line_selection_candidates_rejected"],
        "avg_probability_edge": _avg_decimal(edge_values),
        "avg_expected_value_net": _avg_decimal(net_ev_values),
        "max_expected_value_net": _max_decimal(net_ev_values),
        "fee_estimate_avg": _avg_decimal(fee_values),
        "zero_trade_reason": zero_trade_reason,
        "trade_policy": prediction_run.trade_policy,
    }
