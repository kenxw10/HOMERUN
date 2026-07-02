from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import KalshiMarket, MlbGame, ModelCandidate, PaperTrade, Position
from app.services.contracts import market_type_from_ticker
from app.services.kalshi import KalshiAPIError, KalshiClient, derive_orderbook_prices
from app.services.market_sync import _market_status, _update_market_fields
from app.services.paper_epoch import get_or_create_active_paper_epoch
from app.services.portfolio import create_balance_snapshot
from app.services.settlement import first_five_complete, is_first_five_market
from app.time_utils import utc_now


def _mark_from_orderbook(orderbook: dict[str, object], market: KalshiMarket | None, trade: PaperTrade) -> Decimal | None:
    derived = derive_orderbook_prices(orderbook)
    if market is not None:
        market.best_yes_bid = derived["best_yes_bid"]
        market.best_no_bid = derived["best_no_bid"]
        market.implied_yes_ask = derived["implied_yes_ask"]
        market.implied_no_ask = derived["implied_no_ask"]
        market.market_price_updated_at = utc_now()
        market.orderbook_raw = orderbook

    if trade.contract_side.lower() == "yes":
        return derived["best_yes_bid"]
    return derived["best_no_bid"]


def _mark_from_market(market: KalshiMarket | None, trade: PaperTrade) -> Decimal | None:
    if market is None:
        return None
    if trade.contract_side.lower() == "yes":
        for value in (market.best_yes_bid, market.yes_bid, market.last_price):
            if value is not None:
                return value
    else:
        for value in (market.best_no_bid, market.no_bid):
            if value is not None:
                return value
        if market.last_price is not None:
            complement = Decimal("1.0000") - market.last_price
            if complement >= Decimal("0"):
                return complement
    return None


def _chunks(values: list[str], size: int = 50):
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _markets_by_ticker(client: KalshiClient, tickers: list[str]) -> tuple[dict[str, dict[str, object]], list[dict[str, object]]]:
    markets: dict[str, dict[str, object]] = {}
    errors: list[dict[str, object]] = []
    for chunk in _chunks(tickers):
        try:
            payload = client.get_markets_by_tickers(chunk)
        except KalshiAPIError as exc:
            errors.append({"tickers": chunk, "error": exc.to_detail()})
            continue
        except Exception as exc:
            errors.append({"tickers": chunk, "error": {"message": str(exc), "type": exc.__class__.__name__}})
            continue
        for market in payload.get("markets") or []:
            if isinstance(market, dict) and market.get("ticker"):
                markets[str(market["ticker"]).upper()] = market
    return markets, errors


def refresh_open_position_prices(
    session: Session,
    *,
    client: KalshiClient | None = None,
    include_archived: bool = False,
) -> dict[str, object]:
    settings = get_settings()
    if not settings.open_position_price_refresh_enabled:
        return {
            "checked": 0,
            "updated": 0,
            "skipped": 0,
            "errors": [],
            "snapshot_id": None,
            "skipped_reason": "OPEN_POSITION_PRICE_REFRESH_DISABLED",
        }

    kalshi_client = client or KalshiClient.from_market_data_settings()
    active_epoch = get_or_create_active_paper_epoch(session)
    now = utc_now()
    trade_query = select(PaperTrade).where(PaperTrade.status == "open").order_by(PaperTrade.id.asc())
    if not include_archived:
        trade_query = trade_query.where(PaperTrade.paper_trading_epoch_id == active_epoch.id)
    all_trades = list(session.scalars(trade_query))
    trades = all_trades[: settings.open_position_price_refresh_max_per_run]
    skipped_due_to_limit = max(len(all_trades) - len(trades), 0)
    updated = 0
    skipped = skipped_due_to_limit
    skipped_first_five_settlement_ready = 0
    skipped_closed_f5_market = 0
    errors: list[dict[str, object]] = []
    request_counters = {"market_batch_requests": 0, "orderbook_requests": 0}
    batch_markets: dict[str, dict[str, object]] = {}
    if hasattr(kalshi_client, "get_markets_by_tickers") and trades:
        tickers = sorted({trade.market_ticker for trade in trades})
        batch_markets, batch_errors = _markets_by_ticker(kalshi_client, tickers)
        request_counters["market_batch_requests"] = (len(tickers) + 49) // 50
        errors.extend(batch_errors)

    for trade in trades:
        market = None
        candidate = session.get(ModelCandidate, trade.candidate_id) if trade.candidate_id is not None else None
        game = session.get(MlbGame, candidate.mlb_game_id) if candidate is not None and candidate.mlb_game_id else None
        market_type = market_type_from_ticker(
            trade.market_ticker,
            trade.market_family or (candidate.market_type if candidate is not None else None),
        )
        first_five_trade = is_first_five_market(market_type, trade.inning_scope)
        if first_five_trade and first_five_complete(game):
            skipped += 1
            skipped_first_five_settlement_ready += 1
            continue

        if trade.candidate_id is not None:
            market = session.scalar(
                select(KalshiMarket)
                .join(ModelCandidate, ModelCandidate.kalshi_market_id == KalshiMarket.id)
                .where(ModelCandidate.id == trade.candidate_id)
                .limit(1)
            )
        if market is None:
            market = session.scalar(select(KalshiMarket).where(KalshiMarket.ticker == trade.market_ticker).limit(1))

        mark = None
        payload = batch_markets.get(trade.market_ticker.upper())
        if payload is not None:
            if market is None:
                market = KalshiMarket(
                    ticker=trade.market_ticker,
                    kalshi_market_id=str(payload.get("id") or payload.get("market_id") or trade.market_ticker),
                    title=str(payload.get("title") or trade.market_ticker),
                )
            _update_market_fields(market, payload, trade.market_ticker, _market_status(payload))
            market.market_price_updated_at = now
            market.market_data_source = "rest"
            mark = _mark_from_market(market, trade)
        if mark is None and hasattr(kalshi_client, "get_orderbook"):
            try:
                request_counters["orderbook_requests"] += 1
                orderbook = kalshi_client.get_orderbook(trade.market_ticker)
                mark = _mark_from_orderbook(orderbook, market, trade)
            except KalshiAPIError as exc:
                errors.append({"market_ticker": trade.market_ticker, "error": exc.to_detail()})
            except Exception as exc:
                errors.append(
                    {
                        "market_ticker": trade.market_ticker,
                        "error": {"message": str(exc), "type": exc.__class__.__name__},
                    }
                )

        if mark is None:
            skipped += 1
            continue
        if first_five_trade and not first_five_complete(game):
            market_status = str((market.status if market is not None else "") or "").strip().lower()
            if market_status and market_status != "open":
                skipped += 1
                skipped_closed_f5_market += 1
                continue

        if market is not None:
            market.market_price_updated_at = now
            market.market_data_source = "rest"
            session.add(market)
        trade.current_price = mark
        trade.current_price_updated_at = now
        session.add(trade)
        matching_positions = list(
            session.scalars(
                select(Position)
                .where(Position.status == "open")
                .where(Position.market_ticker == trade.market_ticker)
                .where(Position.contract_side == trade.contract_side)
            )
        )
        for position in matching_positions:
            position.current_price = mark
            session.add(position)
        if market is not None:
            session.add(market)
        updated += 1

    snapshot = create_balance_snapshot(session, source="open_position_price_refresh", epoch=active_epoch)
    session.commit()
    return {
        "checked": len(trades),
        "updated": updated,
        "skipped": skipped,
        "skipped_due_to_limit": skipped_due_to_limit,
        "skipped_first_five_settlement_ready": skipped_first_five_settlement_ready,
        "skipped_closed_f5_market": skipped_closed_f5_market,
        "errors": errors,
        "snapshot_id": snapshot.id,
        "last_marked_at": now.isoformat(),
        "request_counters": request_counters,
        "kalshi_request_count": getattr(kalshi_client, "request_count", None),
        "rate_limited_count": getattr(kalshi_client, "rate_limited_count", None),
    }
