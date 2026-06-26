from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

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

ACTIVE_SLATE_LOOKAHEAD = timedelta(days=21)
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


def _settlement_status(mapping: MarketMapping, market: KalshiMarket) -> str | None:
    return mapping.settlement_rule_status or market.settlement_rule_status


def _base_decision(
    mapping: MarketMapping,
    game: MlbGame,
    market: KalshiMarket,
    market_type: str,
    minutes_to_start: int,
    price: Decimal | None,
    probability: Decimal | None,
    net_ev: Decimal | None,
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
    if price is None:
        return "no_trade_missing_price"
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
    if data_quality is None or data_quality < settings.paper_min_data_quality:
        return "no_trade_low_data_quality"
    if push_probability is not None and push_probability > Decimal("0") and market_type.endswith(("spread", "total")):
        return "no_trade_push_possible"
    if probability is None:
        return "no_trade_missing_probability"
    probability_edge = probability - price
    if net_ev is None or net_ev < settings.paper_min_net_ev:
        return "no_trade_edge_too_low"
    if probability_edge < settings.paper_min_prob_edge:
        return "no_trade_probability_edge_low"
    if settings.paper_require_calibrated_for_trade and calibration_status != "calibrated":
        return "no_trade_uncalibrated_probability"
    if not settings.paper_candidate_engine_enabled:
        return "candidate_only"
    if settings.safe_execution_posture:
        return "eligible_for_paper_trade"
    return "candidate_only"


def _today_trade_counts(
    session: Session,
    start: datetime,
    end: datetime,
) -> tuple[int, dict[int, int], dict[str, int], set[tuple[int, str]], set[str]]:
    rows = list(
        session.execute(
            select(PaperTrade, ModelCandidate)
            .outerjoin(ModelCandidate, PaperTrade.candidate_id == ModelCandidate.id)
            .where(PaperTrade.entry_time >= start)
            .where(PaperTrade.entry_time < end)
        )
    )
    game_counts: dict[int, int] = {}
    family_counts: dict[str, int] = {}
    game_family_pairs: set[tuple[int, str]] = set()
    market_tickers: set[str] = set()
    for trade, candidate in rows:
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


def _apply_trade_caps(
    session: Session,
    intents: list[TradeIntent],
    day_start: datetime,
    day_end: datetime,
) -> tuple[list[TradeIntent], dict[str, int]]:
    settings = get_settings()
    existing_slate, game_counts, family_counts, game_family_pairs, market_tickers = _today_trade_counts(session, day_start, day_end)
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


def generate_candidates(session: Session) -> dict[str, object]:
    settings = get_settings()
    now = utc_now()
    day, day_start, day_end = _candidate_day_bounds(now)
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
            "paper_max_open_positions": settings.paper_max_open_positions,
            "paper_min_net_ev": float(settings.paper_min_net_ev),
            "paper_min_prob_edge": float(settings.paper_min_prob_edge),
            "paper_min_data_quality": float(settings.paper_min_data_quality),
            "paper_require_calibrated_for_trade": settings.paper_require_calibrated_for_trade,
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
        .where(MlbGame.scheduled_start < now + ACTIVE_SLATE_LOOKAHEAD)
    ).all()

    trade_intents: list[TradeIntent] = []
    evaluated_candidates: list[ModelCandidate] = []

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
        gross_ev = (probability - price).quantize(Decimal("0.000001")) if price is not None else None
        fee = Decimal("0.000000")
        net_ev = (gross_ev - fee).quantize(Decimal("0.000001")) if gross_ev is not None else None
        decision = _base_decision(
            mapping,
            game,
            market,
            market_type,
            minutes_to_start,
            price,
            probability,
            net_ev,
            model_score.data_quality,
            model_score.calibration_status,
            model_score.push_probability,
        )

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
        candidate.model_version_tag = MATURE_MODEL_TAG
        candidate.feature_version = FEATURE_VERSION
        candidate.training_eligible = model_score.training_eligible
        candidate.training_exclusion_reason = model_score.training_exclusion_reason
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
            data_quality=candidate.data_quality,
            calibration_status=candidate.calibration_status,
            decision_reason=decision,
            raw_output=model_score.rationale,
        )
        session.add(output)

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

    selected_trades, cap_counts = _apply_trade_caps(session, trade_intents, day_start, day_end)
    for intent in trade_intents:
        output = session.scalar(
            select(ModelPredictionOutput)
            .where(ModelPredictionOutput.candidate_id == intent.candidate.id)
            .where(ModelPredictionOutput.prediction_run_id == prediction_run.id)
            .limit(1)
        )
        if output is not None:
            output.decision_reason = intent.candidate.decision
            session.add(output)

    for rank, intent in enumerate(selected_trades, start=1):
        candidate = intent.candidate
        existing_trade = session.scalar(select(PaperTrade).where(PaperTrade.candidate_id == candidate.id))
        if existing_trade is not None:
            candidate.decision = "candidate_only_existing_trade"
            session.add(candidate)
            output = session.scalar(
                select(ModelPredictionOutput)
                .where(ModelPredictionOutput.candidate_id == candidate.id)
                .where(ModelPredictionOutput.prediction_run_id == prediction_run.id)
                .limit(1)
            )
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
        output = session.scalar(
            select(ModelPredictionOutput)
            .where(ModelPredictionOutput.candidate_id == candidate.id)
            .where(ModelPredictionOutput.prediction_run_id == prediction_run.id)
            .limit(1)
        )
        if output is not None:
            output.trade_rank = rank
            output.decision_reason = "paper_trade"
            session.add(output)
        paper_trades += 1

    decision_counts: dict[str, int] = {}
    for candidate in evaluated_candidates:
        decision = candidate.decision or "unknown"
        decision_counts[decision] = decision_counts.get(decision, 0) + 1

    snapshot = create_balance_snapshot(session, source="candidate_engine")
    prediction_run.completed_at = now
    prediction_run.status = "completed"
    prediction_run.candidates_evaluated = created_or_updated
    prediction_run.trades_created = paper_trades
    prediction_run.summary = {
        "decision_counts": decision_counts,
        "cap_counts": cap_counts,
        "eligible_trade_intents": len(trade_intents),
        "paper_trades": paper_trades,
    }
    session.add(prediction_run)
    session.commit()
    return {
        "date": int(day.strftime("%Y%m%d")),
        "candidates": created_or_updated,
        "paper_trades": paper_trades,
        "model_version": model_version.version_tag,
        "feature_version": FEATURE_VERSION,
        "prediction_run_id": prediction_run.id,
        "snapshot_id": snapshot.id,
        "decision_counts": decision_counts,
        "cap_counts": cap_counts,
        "trade_policy": prediction_run.trade_policy,
    }
