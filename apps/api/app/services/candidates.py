from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import KalshiMarket, MarketMapping, MlbGame, ModelCandidate, PaperTrade
from app.services.mapping import infer_market_type, sync_market_mappings
from app.time_utils import classify_time_bucket, ensure_aware_utc, get_dashboard_zone, utc_now

TEMPORARY_EDGE_THRESHOLD = Decimal("0.0500")
ACTIVE_SLATE_LOOKAHEAD = timedelta(days=21)


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


def _decision(
    mapping: MarketMapping,
    market: KalshiMarket,
    market_type: str,
    minutes_to_start: int,
    price: Decimal | None,
    net_ev: Decimal | None,
) -> str:
    settings = get_settings()
    if mapping.mapping_status == "needs_review" or (mapping.confidence or Decimal("0")) < Decimal("0.55"):
        return "no_trade_mapping_uncertain"
    if market_type == "unknown":
        return "no_trade_unsupported_market_type"
    if minutes_to_start <= 0:
        return "no_trade_game_started"
    if price is None:
        return "no_trade_missing_price"
    if market.status.lower() in {"closed", "settled", "finalized", "expired"}:
        return "no_trade_market_closed"
    if net_ev is None or net_ev < TEMPORARY_EDGE_THRESHOLD:
        return "no_trade_edge_too_low"
    if not settings.paper_candidate_engine_enabled:
        return "candidate_only"
    if settings.safe_execution_posture:
        return "paper_trade"
    return "candidate_only"


def generate_candidates(session: Session) -> dict[str, int]:
    sync_market_mappings(session)
    settings = get_settings()
    now = utc_now()
    day, day_start, day_end = _candidate_day_bounds(now)
    created_or_updated = 0
    paper_trades = 0

    mappings = session.execute(
        select(MarketMapping, MlbGame, KalshiMarket)
        .join(MlbGame, MarketMapping.mlb_game_id == MlbGame.id)
        .join(KalshiMarket, MarketMapping.kalshi_market_id == KalshiMarket.id)
        .where(MarketMapping.mapping_status.in_(["candidate", "confirmed", "needs_review"]))
        .where(MlbGame.scheduled_start > now)
        .where(MlbGame.scheduled_start < now + ACTIVE_SLATE_LOOKAHEAD)
    ).all()

    for mapping, game, market in mappings:
        minutes_to_start = int((ensure_aware_utc(game.scheduled_start) - now).total_seconds() / 60)
        bucket = classify_time_bucket(minutes_to_start)
        text = " ".join(value or "" for value in (market.title, market.subtitle, market.rules))
        market_type = infer_market_type(text)
        probability = Decimal("0.500000")
        price = _market_yes_price(market)
        gross_ev = (probability - price).quantize(Decimal("0.000001")) if price is not None else None
        fee = Decimal("0.000000")
        net_ev = (gross_ev - fee).quantize(Decimal("0.000001")) if gross_ev is not None else None
        decision = _decision(mapping, market, market_type, minutes_to_start, price, net_ev)

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
        contract_side = "yes"
        open_trade_for_market = _open_trade_for_market(session, market.ticker, contract_side)
        candidate = existing or ModelCandidate(
            mapping_id=mapping.id,
            mlb_game_id=game.id,
            kalshi_market_id=market.id,
            evaluated_at=now,
        )
        if open_trade_for_market is not None and price is not None:
            open_trade_for_market.current_price = price
            session.add(open_trade_for_market)
        if (traded_candidate_ids or open_trade_for_market is not None) and decision == "paper_trade":
            decision = "candidate_only_existing_trade"
        candidate.model_version_id = None
        candidate.evaluated_at = now
        candidate.features = {
            "home_team": game.home_team,
            "away_team": game.away_team,
            "scheduled_start": game.scheduled_start.isoformat(),
            "mapping_confidence": float(mapping.confidence or 0),
            "market_status": market.status,
            "best_yes_bid": float(market.best_yes_bid) if market.best_yes_bid is not None else None,
            "implied_yes_ask": float(market.implied_yes_ask) if market.implied_yes_ask is not None else None,
        }
        candidate.probability = probability
        candidate.model_probability = probability
        candidate.fair_value = Decimal("0.5000")
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
        session.add(candidate)
        session.flush()
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
                )
                session.add(trade)
                session.flush()
                paper_trades += 1

    session.commit()
    return {"date": int(day.strftime("%Y%m%d")), "candidates": created_or_updated, "paper_trades": paper_trades}
