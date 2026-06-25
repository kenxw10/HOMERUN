from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import KalshiMarket, MarketMapping, MlbGame, ModelCandidate, PaperTrade, Position, Settlement
from app.services.contracts import SUPPORTED_MARKET_FAMILY, market_type_from_ticker, selected_team_from_ticker
from app.services.portfolio import create_balance_snapshot
from app.time_utils import ensure_aware_utc, get_dashboard_zone, utc_now

FINAL_STATUS_TOKENS = ("final", "game over", "completed")
VOID_STATUS_TOKENS = ("cancel", "void")


def _target_bounds(target_date: date | None) -> tuple[datetime, datetime] | None:
    if target_date is None:
        return None
    local_start = datetime.combine(target_date, time.min, tzinfo=get_dashboard_zone())
    start = ensure_aware_utc(local_start)
    return start, start + timedelta(days=1)


def _status_kind(status: str) -> str:
    lowered = status.strip().lower()
    if any(token in lowered for token in VOID_STATUS_TOKENS):
        return "void"
    if any(token in lowered for token in FINAL_STATUS_TOKENS):
        return "final"
    return "open"


def _winner_code(game: MlbGame) -> str | None:
    if game.home_score is None or game.away_score is None:
        return None
    if game.home_score == game.away_score:
        return "PUSH"
    if game.home_score > game.away_score:
        return (game.home_abbreviation or "").upper()
    return (game.away_abbreviation or "").upper()


def _game_team_codes(game: MlbGame) -> set[str]:
    return {code for code in ((game.home_abbreviation or "").upper(), (game.away_abbreviation or "").upper()) if code}


def _has_trusted_selection(game: MlbGame, market_ticker: str) -> bool:
    selected = selected_team_from_ticker(market_ticker)
    team_codes = _game_team_codes(game)
    return selected is not None and bool(team_codes) and selected in team_codes


def _skip_reason(game: MlbGame, market_ticker: str, market_type: str) -> str:
    if market_type != SUPPORTED_MARKET_FAMILY:
        return "unsupported"
    if _status_kind(game.status) == "open":
        return "not_final"
    if not _has_trusted_selection(game, market_ticker):
        return "invalid_selection"
    return "not_final"


def _contract_outcome(
    game: MlbGame,
    *,
    market_ticker: str,
    contract_side: str | None,
    market_type: str,
) -> tuple[str, str] | None:
    if market_type != SUPPORTED_MARKET_FAMILY:
        return None

    status_kind = _status_kind(game.status)
    if status_kind == "open":
        return None
    if status_kind == "void":
        return "void", "VOID"

    winner = _winner_code(game)
    if winner is None or not _has_trusted_selection(game, market_ticker):
        return None
    selected = selected_team_from_ticker(market_ticker)
    if winner == "PUSH":
        return "push", "PUSH"

    selected_won = selected == winner
    side = (contract_side or "yes").lower()
    won = selected_won if side == "yes" else not selected_won
    return ("win", "WIN") if won else ("loss", "LOSS")


def _trade_outcome(game: MlbGame, trade: PaperTrade, market_type: str) -> tuple[str, str] | None:
    return _contract_outcome(
        game,
        market_ticker=trade.market_ticker,
        contract_side=trade.contract_side,
        market_type=market_type,
    )


def _candidate_outcome(game: MlbGame, candidate: ModelCandidate, market: KalshiMarket) -> tuple[str, str] | None:
    return _contract_outcome(
        game,
        market_ticker=market.ticker,
        contract_side=candidate.contract_side,
        market_type=market_type_from_ticker(market.ticker, candidate.market_type),
    )


def _settlement_amounts(trade: PaperTrade, outcome: str) -> tuple[Decimal, Decimal, Decimal]:
    quantity = Decimal(trade.quantity)
    cost = trade.entry_price * quantity
    fee = Decimal("0.00")
    if outcome == "win":
        payout = quantity
        realized = payout - cost - fee
        exit_price = Decimal("1.0000")
    elif outcome == "loss":
        payout = Decimal("0.00")
        realized = -cost - fee
        exit_price = Decimal("0.0000")
    else:
        payout = cost
        realized = Decimal("0.00")
        exit_price = trade.entry_price
    return payout.quantize(Decimal("0.01")), realized.quantize(Decimal("0.01")), exit_price


def _open_position_for_trade(session: Session, trade: PaperTrade) -> Position | None:
    return session.scalar(
        select(Position)
        .where(Position.market_ticker == trade.market_ticker)
        .where(Position.contract_side == trade.contract_side)
        .where(Position.status == "open")
        .order_by(Position.opened_at.desc(), Position.id.desc())
        .limit(1)
    )


def settle_paper_trades(
    session: Session,
    target_date: date | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    settled_at = now or utc_now()
    bounds = _target_bounds(target_date)
    candidate_query = (
        select(ModelCandidate, MarketMapping, MlbGame, KalshiMarket)
        .join(MarketMapping, ModelCandidate.mapping_id == MarketMapping.id)
        .join(MlbGame, MarketMapping.mlb_game_id == MlbGame.id)
        .join(KalshiMarket, MarketMapping.kalshi_market_id == KalshiMarket.id)
    )
    if bounds is not None:
        start, end = bounds
        candidate_query = candidate_query.where(MlbGame.scheduled_start >= start).where(MlbGame.scheduled_start < end)

    candidate_rows = session.execute(candidate_query).all()

    query = (
        select(PaperTrade, ModelCandidate, MarketMapping, MlbGame, KalshiMarket)
        .join(ModelCandidate, PaperTrade.candidate_id == ModelCandidate.id)
        .join(MarketMapping, ModelCandidate.mapping_id == MarketMapping.id)
        .join(MlbGame, MarketMapping.mlb_game_id == MlbGame.id)
        .join(KalshiMarket, MarketMapping.kalshi_market_id == KalshiMarket.id)
        .where(PaperTrade.status == "open")
    )
    if bounds is not None:
        start, end = bounds
        query = query.where(MlbGame.scheduled_start >= start).where(MlbGame.scheduled_start < end)

    rows = session.execute(query).all()
    result = {
        "checked": len(rows),
        "settled": 0,
        "voided": 0,
        "skipped_not_final": 0,
        "skipped_unsupported": 0,
        "skipped_invalid_selection": 0,
        "already_settled": 0,
        "candidate_labels_checked": len(candidate_rows),
        "candidate_labels_created": 0,
        "candidate_labels_already_set": 0,
        "candidate_labels_skipped_not_final": 0,
        "candidate_labels_skipped_unsupported": 0,
        "candidate_labels_skipped_invalid_selection": 0,
        "snapshot_id": None,
    }

    for candidate, _mapping, game, market in candidate_rows:
        if candidate.outcome is not None:
            result["candidate_labels_already_set"] = int(result["candidate_labels_already_set"]) + 1
            continue

        market_type = market_type_from_ticker(market.ticker, candidate.market_type)
        outcome = _candidate_outcome(game, candidate, market)
        if outcome is None:
            reason = _skip_reason(game, market.ticker, market_type)
            if reason == "unsupported":
                result["candidate_labels_skipped_unsupported"] = int(result["candidate_labels_skipped_unsupported"]) + 1
            elif reason == "invalid_selection":
                result["candidate_labels_skipped_invalid_selection"] = (
                    int(result["candidate_labels_skipped_invalid_selection"]) + 1
                )
            else:
                result["candidate_labels_skipped_not_final"] = int(result["candidate_labels_skipped_not_final"]) + 1
            continue

        outcome_value, _resolution = outcome
        candidate.outcome = outcome_value
        candidate.outcome_source = "mlb_results_sync"
        candidate.resolved_at = settled_at
        session.add(candidate)
        result["candidate_labels_created"] = int(result["candidate_labels_created"]) + 1

    for trade, candidate, _mapping, game, market in rows:
        existing = session.scalar(select(Settlement).where(Settlement.paper_trade_id == trade.id))
        if existing is not None:
            result["already_settled"] = int(result["already_settled"]) + 1
            continue

        market_type = market_type_from_ticker(market.ticker, candidate.market_type)
        outcome = _trade_outcome(game, trade, market_type)
        if outcome is None:
            reason = _skip_reason(game, trade.market_ticker, market_type)
            if reason == "unsupported":
                result["skipped_unsupported"] = int(result["skipped_unsupported"]) + 1
            elif reason == "invalid_selection":
                result["skipped_invalid_selection"] = int(result["skipped_invalid_selection"]) + 1
            else:
                result["skipped_not_final"] = int(result["skipped_not_final"]) + 1
            continue

        outcome_value, resolution = outcome
        payout, realized, exit_price = _settlement_amounts(trade, outcome_value)
        fee = Decimal("0.00")
        terminal_status = "void" if outcome_value == "void" else "settled"

        trade.status = terminal_status
        trade.outcome = outcome_value
        trade.resolution = resolution
        trade.realized_pnl = realized
        trade.exit_price = exit_price
        trade.current_price = exit_price
        trade.exit_time = settled_at
        trade.settled_at = settled_at
        trade.fee_paid = fee
        session.add(trade)

        candidate.outcome = outcome_value
        candidate.outcome_source = "mlb_results_sync"
        candidate.resolved_at = settled_at
        session.add(candidate)

        position = _open_position_for_trade(session, trade)
        if position is not None:
            position.status = terminal_status
            position.resolution = resolution
            position.current_price = exit_price
            position.closed_at = settled_at
            session.add(position)

        settlement = Settlement(
            position_id=position.id if position else None,
            paper_trade_id=trade.id,
            settled_at=settled_at,
            resolution=resolution,
            outcome=outcome_value,
            payout=payout,
            fee_paid=fee,
            realized_pnl=realized,
        )
        session.add(settlement)
        if outcome_value == "void":
            result["voided"] = int(result["voided"]) + 1
        else:
            result["settled"] = int(result["settled"]) + 1

    snapshot = create_balance_snapshot(session, source="settlement_sync")
    result["snapshot_id"] = snapshot.id
    session.commit()
    return result
