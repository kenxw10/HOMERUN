from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import FeatureSnapshot, KalshiMarket, MarketMapping, MlbGame, ModelCandidate, PaperTrade
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
from app.services.modeling import get_or_create_heuristic_model_version, score_candidate_probability
from app.services.portfolio import create_balance_snapshot
from app.time_utils import classify_time_bucket, ensure_aware_utc, get_dashboard_zone, utc_now

TEMPORARY_EDGE_THRESHOLD = Decimal("0.0500")
PR3B_FEATURE_VERSION = "market_family_wire_v1_pre_full_model"
PR3B_MODEL_VERSION_TAG = "baseline_market_family_wire_v1"
ACTIVE_SLATE_LOOKAHEAD = timedelta(days=21)
TRADABLE_MARKET_STATUSES = {"active", "open"}
PLAYABLE_GAME_STATUSES = {"pre-game", "preview", "scheduled", "warmup"}


def _candidate_day_bounds(now: datetime) -> tuple[date, datetime, datetime]:
    dashboard_zone = get_dashboard_zone()
    day = now.astimezone(dashboard_zone).date()
    day_start = ensure_aware_utc(datetime.combine(day, time.min, tzinfo=dashboard_zone))
    return day, day_start, day_start + timedelta(days=1)


def _market_yes_price(market: KalshiMarket) -> Decimal | None:
    for value in (
        market.implied_yes_ask,
        market.yes_ask,
    ):
        if value is not None:
            return value
    return None


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


def _decision(
    mapping: MarketMapping,
    game: MlbGame,
    market: KalshiMarket,
    market_type: str,
    minutes_to_start: int,
    price: Decimal | None,
    net_ev: Decimal | None,
) -> str:
    settings = get_settings()
    if mapping.mapping_status == "needs_review" or (mapping.confidence or Decimal("0")) < Decimal("0.55"):
        return "no_trade_mapping_uncertain"
    if market_type not in PAPER_SUPPORTED_MARKET_FAMILIES:
        return "no_trade_unsupported_family"
    settlement_status = mapping.settlement_rule_status or market.settlement_rule_status
    if market_type != FULL_GAME_WINNER and settlement_status != "paper_supported":
        if mapping.line_value is None and market.line_value is None and market_type.endswith(("spread", "total")):
            return "no_trade_missing_line"
        return "no_trade_parse_uncertain"
    if minutes_to_start <= 0:
        return "no_trade_game_started"
    if price is None:
        return "no_trade_missing_price"
    if market.status.strip().lower() not in TRADABLE_MARKET_STATUSES:
        return "no_trade_market_closed"
    if net_ev is None or net_ev < TEMPORARY_EDGE_THRESHOLD:
        return "no_trade_edge_too_low"
    if not settings.paper_candidate_engine_enabled:
        return "candidate_only"
    selection_required = market_type in {FULL_GAME_WINNER, "full_game_spread", "first_five_winner", "first_five_spread"}
    if settings.safe_execution_posture and selection_required and not _has_trusted_candidate_selection(mapping, game, market):
        if market_type == "first_five_winner" and (mapping.selection_code or market.selection_code) == "TIE":
            return "paper_trade"
        return "no_trade_untrusted_selection"
    if settings.safe_execution_posture:
        return "paper_trade"
    return "candidate_only"


def generate_candidates(session: Session) -> dict[str, int]:
    settings = get_settings()
    now = utc_now()
    day, day_start, day_end = _candidate_day_bounds(now)
    created_or_updated = 0
    paper_trades = 0
    model_version = get_or_create_heuristic_model_version(session)

    mappings = session.execute(
        select(MarketMapping, MlbGame, KalshiMarket)
        .join(MlbGame, MarketMapping.mlb_game_id == MlbGame.id)
        .join(KalshiMarket, MarketMapping.kalshi_market_id == KalshiMarket.id)
        .where(MarketMapping.mapping_status.in_(["candidate", "confirmed", "needs_review"]))
        .where(func.lower(MlbGame.status).in_(PLAYABLE_GAME_STATUSES))
        .where(MlbGame.scheduled_start > now)
        .where(MlbGame.scheduled_start < now + ACTIVE_SLATE_LOOKAHEAD)
    ).all()

    for mapping, game, market in mappings:
        minutes_to_start = int((ensure_aware_utc(game.scheduled_start) - now).total_seconds() / 60)
        bucket = classify_time_bucket(minutes_to_start)
        market_type = (
            mapping.market_type
            or market.market_type
            or market_type_from_ticker(market.ticker, infer_market_type(_market_classification_text(market)))
        )
        price = _market_yes_price(market)
        contract_side = "yes"
        features = build_feature_snapshot(game, market, mapping)
        labels = contract_labels(game=game, market=market, market_ticker=market.ticker, market_type=market_type)
        if market_type == FULL_GAME_WINNER:
            model_score = score_candidate_probability(features, contract_side)
            probability = model_score.probability
            fair_value = model_score.fair_value
            scoring_rationale = model_score.rationale
            model_version_tag = model_version.version_tag
            feature_version = FEATURE_VERSION
            training_eligible = True
        else:
            probability = Decimal("0.500000")
            fair_value = Decimal("0.5000")
            scoring_rationale = {
                "model_version": PR3B_MODEL_VERSION_TAG,
                "feature_version": PR3B_FEATURE_VERSION,
                "model_family": market_type,
                "reason": "pr3b_market_family_plumbing_baseline",
                "uses_market_price": False,
                "base_probability": 0.5,
            }
            model_version_tag = PR3B_MODEL_VERSION_TAG
            feature_version = PR3B_FEATURE_VERSION
            training_eligible = False
        gross_ev = (probability - price).quantize(Decimal("0.000001")) if price is not None else None
        fee = Decimal("0.000000")
        net_ev = (gross_ev - fee).quantize(Decimal("0.000001")) if gross_ev is not None else None
        decision = _decision(mapping, game, market, market_type, minutes_to_start, price, net_ev)

        existing_candidates = list(
            session.scalars(
                select(ModelCandidate)
                .where(ModelCandidate.mapping_id == mapping.id)
                .where(ModelCandidate.time_bucket == bucket)
                .where(ModelCandidate.evaluated_at >= day_start)
                .where(ModelCandidate.evaluated_at < day_end)
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
        if (traded_candidate_ids or open_trade_for_market is not None) and decision == "paper_trade":
            decision = "candidate_only_existing_trade"
        candidate.model_version_id = model_version.id
        candidate.evaluated_at = now
        candidate.features = features
        candidate.probability = probability
        candidate.model_probability = probability
        candidate.fair_value = fair_value
        candidate.market_price = price
        candidate.executable_price = price
        candidate.expected_value = gross_ev
        candidate.fee_estimate = fee
        candidate.net_expected_value = net_ev
        candidate.market_type = market_type
        candidate.time_bucket = bucket
        candidate.time_to_start_minutes = minutes_to_start
        candidate.contract_side = contract_side
        candidate.decision = decision
        candidate.model_version_tag = model_version_tag
        candidate.feature_version = feature_version
        candidate.training_eligible = training_eligible
        candidate.scoring_rationale = scoring_rationale
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
                source=feature_version,
            )
        )
        created_or_updated += 1

        if decision == "paper_trade":
            existing_trade = session.scalar(select(PaperTrade).where(PaperTrade.candidate_id == candidate.id))
            if existing_trade is None and price is not None:
                trade = PaperTrade(
                    candidate_id=candidate.id,
                    market_ticker=market.ticker,
                    contract_side="yes",
                    entry_price=price,
                    current_price=price,
                    quantity=settings.default_paper_contracts,
                    entry_time=now,
                    status="open",
                    expected_value=net_ev,
                    market_display=labels.market_display,
                    selection_display=labels.selection_display,
                    matchup_display=labels.matchup_display,
                    contract_display=labels.contract_display,
                    market_family=candidate.market_family,
                    line_value=candidate.line_value,
                    selection_code=candidate.selection_code,
                    over_under_side=candidate.over_under_side,
                    inning_scope=candidate.inning_scope,
                    settlement_rule_status=candidate.settlement_rule_status,
                    training_eligible=training_eligible,
                )
                session.add(trade)
                session.flush()
                paper_trades += 1

    snapshot = create_balance_snapshot(session, source="candidate_engine")
    session.commit()
    return {
        "date": int(day.strftime("%Y%m%d")),
        "candidates": created_or_updated,
        "paper_trades": paper_trades,
        "model_version": model_version.version_tag,
        "snapshot_id": snapshot.id,
    }
