from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import KalshiMarket, MarketMapping, MlbGame, ModelCandidate, PaperTrade, Position, Settlement
from app.services.contracts import (
    FIRST_FIVE_SPREAD,
    FIRST_FIVE_TOTAL,
    FIRST_FIVE_WINNER,
    FULL_GAME_SPREAD,
    FULL_GAME_TOTAL,
    FULL_GAME_WINNER,
    PAPER_SUPPORTED_MARKET_FAMILIES,
    has_trusted_selection,
    market_type_from_ticker,
    selected_team_from_ticker,
)
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


def _first_five_runs(game: MlbGame) -> tuple[int, int] | None:
    raw = game.raw_payload or {}
    linescore = raw.get("linescore") if isinstance(raw, dict) else None
    innings = linescore.get("innings") if isinstance(linescore, dict) else None
    if not isinstance(innings, list) or len(innings) < 5:
        return None
    away = 0
    home = 0
    for inning in innings[:5]:
        if not isinstance(inning, dict):
            return None
        away_runs = (inning.get("away") or {}).get("runs") if isinstance(inning.get("away"), dict) else None
        home_runs = (inning.get("home") or {}).get("runs") if isinstance(inning.get("home"), dict) else None
        if away_runs is None or home_runs is None:
            return None
        away += int(away_runs)
        home += int(home_runs)
    return away, home


def _score_context(game: MlbGame, inning_scope: str | None) -> tuple[int, int] | None:
    if inning_scope == "first_five":
        return _first_five_runs(game)
    if game.away_score is None or game.home_score is None:
        return None
    return int(game.away_score), int(game.home_score)


def _selected_runs(game: MlbGame, selected: str, scores: tuple[int, int]) -> tuple[int, int] | None:
    away_code = (game.away_abbreviation or "").upper()
    home_code = (game.home_abbreviation or "").upper()
    away_runs, home_runs = scores
    if selected == away_code:
        return away_runs, home_runs
    if selected == home_code:
        return home_runs, away_runs
    return None


def _line_result(value: Decimal) -> tuple[str, str] | None:
    if value > 0:
        return "win", "WIN"
    if value < 0:
        return "loss", "LOSS"
    return "push", "PUSH"


def _first_decimal(*values: Decimal | None) -> Decimal | None:
    for value in values:
        if value is not None:
            return value
    return None


def _first_text(*values: str | None) -> str | None:
    for value in values:
        if value is not None:
            return value
    return None


def _skip_reason(
    game: MlbGame,
    market_ticker: str,
    market_type: str,
    *,
    line_value: Decimal | None = None,
    selection_code: str | None = None,
    over_under_side: str | None = None,
    inning_scope: str | None = None,
    settlement_rule_status: str | None = None,
) -> str:
    if market_type not in PAPER_SUPPORTED_MARKET_FAMILIES:
        return "unsupported"
    if _status_kind(game.status) == "open":
        return "not_final"
    if market_type != FULL_GAME_WINNER and settlement_rule_status != "paper_supported":
        return "parse_uncertain"
    if inning_scope == "first_five" and _first_five_runs(game) is None:
        return "missing_f5_linescore"
    if market_type in {FULL_GAME_SPREAD, FIRST_FIVE_SPREAD} and line_value is None:
        return "missing_line"
    if market_type in {FULL_GAME_TOTAL, FIRST_FIVE_TOTAL} and (line_value is None or over_under_side not in {"over", "under"}):
        return "parse_uncertain"
    if market_type in {FULL_GAME_WINNER, FULL_GAME_SPREAD, FIRST_FIVE_SPREAD} and not has_trusted_selection(game, market_ticker):
        return "invalid_selection"
    if market_type == FIRST_FIVE_WINNER:
        selected = (selection_code or selected_team_from_ticker(market_ticker) or "").upper()
        if selected != "TIE" and not has_trusted_selection(game, market_ticker):
            return "invalid_selection"
    if selection_code is None and market_type in {FULL_GAME_SPREAD, FIRST_FIVE_WINNER, FIRST_FIVE_SPREAD}:
        return "invalid_selection"
    return "not_final"


def _contract_outcome(
    game: MlbGame,
    *,
    market_ticker: str,
    contract_side: str | None,
    market_type: str,
    line_value: Decimal | None = None,
    selection_code: str | None = None,
    over_under_side: str | None = None,
    inning_scope: str | None = None,
    settlement_rule_status: str | None = None,
) -> tuple[str, str] | None:
    if market_type not in PAPER_SUPPORTED_MARKET_FAMILIES:
        return None

    status_kind = _status_kind(game.status)
    if status_kind == "open":
        return None
    if status_kind == "void":
        return "void", "VOID"

    if market_type != FULL_GAME_WINNER and settlement_rule_status != "paper_supported":
        return None

    side = (contract_side or "yes").lower()
    scores = _score_context(game, inning_scope)
    if scores is None:
        return None

    selected = (selection_code or selected_team_from_ticker(market_ticker) or "").upper()
    away_runs, home_runs = scores
    away_code = (game.away_abbreviation or "").upper()
    home_code = (game.home_abbreviation or "").upper()

    if market_type in {FULL_GAME_WINNER, FIRST_FIVE_WINNER}:
        if market_type == FULL_GAME_WINNER:
            winner = _winner_code(game)
            if winner is None or not has_trusted_selection(game, market_ticker):
                return None
            if winner == "PUSH":
                return "push", "PUSH"
            selected_won = selected == winner
        else:
            if away_runs == home_runs:
                winner = "TIE"
            elif away_runs > home_runs:
                winner = away_code
            else:
                winner = home_code
            if selected not in {away_code, home_code, "TIE"}:
                return None
            selected_won = selected == winner
        won = selected_won if side == "yes" else not selected_won
        return ("win", "WIN") if won else ("loss", "LOSS")

    if market_type in {FULL_GAME_SPREAD, FIRST_FIVE_SPREAD}:
        if line_value is None:
            return None
        selected_pair = _selected_runs(game, selected, scores)
        if selected_pair is None:
            return None
        selected_runs, opponent_runs = selected_pair
        yes_result = _line_result(Decimal(selected_runs - opponent_runs) + line_value)
        if yes_result is None:
            return None
        outcome, resolution = yes_result
        if side == "no" and outcome in {"win", "loss"}:
            outcome, resolution = ("loss", "LOSS") if outcome == "win" else ("win", "WIN")
        return outcome, resolution

    if market_type in {FULL_GAME_TOTAL, FIRST_FIVE_TOTAL}:
        if line_value is None or over_under_side not in {"over", "under"}:
            return None
        total_runs = Decimal(away_runs + home_runs)
        if total_runs == line_value:
            return "push", "PUSH"
        yes_won = total_runs > line_value if over_under_side == "over" else total_runs < line_value
        won = yes_won if side == "yes" else not yes_won
        return ("win", "WIN") if won else ("loss", "LOSS")

    return None


def _trade_outcome(
    game: MlbGame,
    trade: PaperTrade,
    market_type: str,
    mapping: MarketMapping,
    market: KalshiMarket,
) -> tuple[str, str] | None:
    return _contract_outcome(
        game,
        market_ticker=trade.market_ticker,
        contract_side=trade.contract_side,
        market_type=market_type,
        line_value=_first_decimal(trade.line_value, mapping.line_value, market.line_value),
        selection_code=_first_text(trade.selection_code, mapping.selection_code, market.selection_code),
        over_under_side=_first_text(trade.over_under_side, mapping.over_under_side, market.over_under_side),
        inning_scope=_first_text(trade.inning_scope, mapping.inning_scope, market.inning_scope),
        settlement_rule_status=_first_text(
            trade.settlement_rule_status,
            mapping.settlement_rule_status,
            market.settlement_rule_status,
        ),
    )


def _candidate_outcome(
    game: MlbGame,
    candidate: ModelCandidate,
    mapping: MarketMapping,
    market: KalshiMarket,
) -> tuple[str, str] | None:
    return _contract_outcome(
        game,
        market_ticker=market.ticker,
        contract_side=candidate.contract_side,
        market_type=market_type_from_ticker(market.ticker, candidate.market_type),
        line_value=_first_decimal(candidate.line_value, mapping.line_value, market.line_value),
        selection_code=_first_text(candidate.selection_code, mapping.selection_code, market.selection_code),
        over_under_side=_first_text(candidate.over_under_side, mapping.over_under_side, market.over_under_side),
        inning_scope=_first_text(candidate.inning_scope, mapping.inning_scope, market.inning_scope),
        settlement_rule_status=_first_text(
            candidate.settlement_rule_status,
            mapping.settlement_rule_status,
            market.settlement_rule_status,
        ),
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
        "skipped_parse_uncertain": 0,
        "skipped_missing_line": 0,
        "skipped_missing_f5_linescore": 0,
        "already_settled": 0,
        "candidate_labels_checked": len(candidate_rows),
        "candidate_labels_created": 0,
        "candidate_labels_already_set": 0,
        "candidate_labels_skipped_not_final": 0,
        "candidate_labels_skipped_unsupported": 0,
        "candidate_labels_skipped_invalid_selection": 0,
        "candidate_labels_skipped_parse_uncertain": 0,
        "candidate_labels_skipped_missing_line": 0,
        "candidate_labels_skipped_missing_f5_linescore": 0,
        "snapshot_id": None,
    }

    for candidate, _mapping, game, market in candidate_rows:
        if candidate.outcome is not None:
            result["candidate_labels_already_set"] = int(result["candidate_labels_already_set"]) + 1
            continue

        market_type = market_type_from_ticker(market.ticker, candidate.market_type)
        outcome = _candidate_outcome(game, candidate, _mapping, market)
        if outcome is None:
            reason = _skip_reason(
                game,
                market.ticker,
                market_type,
                line_value=_first_decimal(candidate.line_value, _mapping.line_value, market.line_value),
                selection_code=_first_text(candidate.selection_code, _mapping.selection_code, market.selection_code),
                over_under_side=_first_text(candidate.over_under_side, _mapping.over_under_side, market.over_under_side),
                inning_scope=_first_text(candidate.inning_scope, _mapping.inning_scope, market.inning_scope),
                settlement_rule_status=_first_text(
                    candidate.settlement_rule_status,
                    _mapping.settlement_rule_status,
                    market.settlement_rule_status,
                ),
            )
            if reason == "unsupported":
                result["candidate_labels_skipped_unsupported"] = int(result["candidate_labels_skipped_unsupported"]) + 1
            elif reason == "invalid_selection":
                result["candidate_labels_skipped_invalid_selection"] = (
                    int(result["candidate_labels_skipped_invalid_selection"]) + 1
                )
            elif reason == "parse_uncertain":
                result["candidate_labels_skipped_parse_uncertain"] = (
                    int(result["candidate_labels_skipped_parse_uncertain"]) + 1
                )
            elif reason == "missing_line":
                result["candidate_labels_skipped_missing_line"] = int(result["candidate_labels_skipped_missing_line"]) + 1
            elif reason == "missing_f5_linescore":
                result["candidate_labels_skipped_missing_f5_linescore"] = (
                    int(result["candidate_labels_skipped_missing_f5_linescore"]) + 1
                )
            else:
                result["candidate_labels_skipped_not_final"] = int(result["candidate_labels_skipped_not_final"]) + 1
            continue

        outcome_value, _resolution = outcome
        if market_type in PAPER_SUPPORTED_MARKET_FAMILIES:
            candidate.market_type = market_type
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
        outcome = _trade_outcome(game, trade, market_type, _mapping, market)
        if outcome is None:
            reason = _skip_reason(
                game,
                trade.market_ticker,
                market_type,
                line_value=_first_decimal(trade.line_value, _mapping.line_value, market.line_value),
                selection_code=_first_text(trade.selection_code, _mapping.selection_code, market.selection_code),
                over_under_side=_first_text(trade.over_under_side, _mapping.over_under_side, market.over_under_side),
                inning_scope=_first_text(trade.inning_scope, _mapping.inning_scope, market.inning_scope),
                settlement_rule_status=_first_text(
                    trade.settlement_rule_status,
                    _mapping.settlement_rule_status,
                    market.settlement_rule_status,
                ),
            )
            if reason == "unsupported":
                result["skipped_unsupported"] = int(result["skipped_unsupported"]) + 1
            elif reason == "invalid_selection":
                result["skipped_invalid_selection"] = int(result["skipped_invalid_selection"]) + 1
            elif reason == "parse_uncertain":
                result["skipped_parse_uncertain"] = int(result["skipped_parse_uncertain"]) + 1
            elif reason == "missing_line":
                result["skipped_missing_line"] = int(result["skipped_missing_line"]) + 1
            elif reason == "missing_f5_linescore":
                result["skipped_missing_f5_linescore"] = int(result["skipped_missing_f5_linescore"]) + 1
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
        if market_type in PAPER_SUPPORTED_MARKET_FAMILIES:
            candidate.market_type = market_type
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
