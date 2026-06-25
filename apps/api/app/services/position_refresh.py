from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import KalshiMarket, ModelCandidate, PaperTrade
from app.services.kalshi import KalshiAPIError, KalshiClient, derive_orderbook_prices
from app.services.portfolio import create_balance_snapshot
from app.time_utils import utc_now


def _first_present(values: tuple[Decimal | None, ...]) -> Decimal | None:
    for value in values:
        if value is not None:
            return value
    return None


def _fallback_mark(market: KalshiMarket | None, contract_side: str) -> Decimal | None:
    if market is None:
        return None

    side = contract_side.lower()
    values = (
        (market.best_yes_bid, market.yes_bid, market.last_price)
        if side == "yes"
        else (market.best_no_bid, market.no_bid, market.last_price)
    )
    return _first_present(values)


def _mark_from_orderbook(orderbook: dict[str, object], market: KalshiMarket | None, trade: PaperTrade) -> Decimal | None:
    derived = derive_orderbook_prices(orderbook)
    if market is not None:
        market.best_yes_bid = derived["best_yes_bid"]
        market.best_no_bid = derived["best_no_bid"]
        market.implied_yes_ask = derived["implied_yes_ask"]
        market.implied_no_ask = derived["implied_no_ask"]
        market.orderbook_raw = orderbook

    if trade.contract_side.lower() == "yes":
        return _first_present((derived["best_yes_bid"], _fallback_mark(market, trade.contract_side)))
    return _first_present((derived["best_no_bid"], _fallback_mark(market, trade.contract_side)))


def refresh_open_position_prices(
    session: Session,
    *,
    client: KalshiClient | None = None,
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

    kalshi_client = client or KalshiClient.from_settings()
    now = utc_now()
    trades = list(session.scalars(select(PaperTrade).where(PaperTrade.status == "open").order_by(PaperTrade.id.asc())))
    updated = 0
    skipped = 0
    errors: list[dict[str, object]] = []

    for trade in trades:
        market = None
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
        try:
            orderbook = kalshi_client.get_orderbook(trade.market_ticker)
            mark = _mark_from_orderbook(orderbook, market, trade)
        except KalshiAPIError as exc:
            errors.append({"market_ticker": trade.market_ticker, "error": exc.to_detail()})
            mark = _fallback_mark(market, trade.contract_side)
        except Exception as exc:
            errors.append(
                {
                    "market_ticker": trade.market_ticker,
                    "error": {"message": str(exc), "type": exc.__class__.__name__},
                }
            )
            mark = _fallback_mark(market, trade.contract_side)

        if mark is None:
            skipped += 1
            continue

        trade.current_price = mark
        trade.current_price_updated_at = now
        session.add(trade)
        if market is not None:
            session.add(market)
        updated += 1

    snapshot = create_balance_snapshot(session, source="open_position_price_refresh")
    session.commit()
    return {
        "checked": len(trades),
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "snapshot_id": snapshot.id,
        "last_marked_at": now.isoformat(),
    }
