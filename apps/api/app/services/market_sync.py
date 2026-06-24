from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import KalshiMarket
from app.services.kalshi import KalshiClient, derive_orderbook_prices
from app.time_utils import parse_datetime, utc_now

BASEBALL_KEYWORDS = ("mlb", "baseball", "world series", "american league", "national league")
DISCOVERABLE_MARKET_STATUSES = {"open", "unopened"}


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except Exception:
        return None
    if parsed > 1:
        parsed = parsed / Decimal("100")
    return parsed.quantize(Decimal("0.0001"))


def _market_text(market: dict[str, Any]) -> str:
    return " ".join(
        str(market.get(key) or "")
        for key in ("title", "subtitle", "rules_primary", "rules_secondary", "event_ticker", "ticker")
    ).lower()


def looks_like_baseball_market(market: dict[str, Any]) -> bool:
    text = _market_text(market)
    return any(keyword in text for keyword in BASEBALL_KEYWORDS)


def _market_status(market: dict[str, Any]) -> str:
    return str(market.get("status") or "").strip().lower()


def _clear_orderbook_prices(row: KalshiMarket) -> None:
    row.best_yes_bid = None
    row.best_no_bid = None
    row.implied_yes_ask = None
    row.implied_no_ask = None


def _update_market_fields(row: KalshiMarket, market: dict[str, Any], ticker: str, status: str) -> None:
    row.kalshi_market_id = str(market.get("id") or market.get("market_id") or ticker)
    row.event_ticker = market.get("event_ticker")
    row.title = market.get("title") or ticker
    row.subtitle = market.get("subtitle")
    row.rules = market.get("rules_primary") or market.get("rules")
    row.yes_subtitle = market.get("yes_sub_title") or market.get("yes_subtitle")
    row.no_subtitle = market.get("no_sub_title") or market.get("no_subtitle")
    row.status = status or "untracked"
    row.open_time = parse_datetime(market.get("open_time"))
    row.close_time = parse_datetime(market.get("close_time"))
    row.occurrence_datetime = parse_datetime(market.get("expected_expiration_time") or market.get("occurrence_datetime"))
    row.resolve_time = parse_datetime(market.get("expiration_time") or market.get("resolve_time"))
    row.yes_bid = _decimal(market.get("yes_bid"))
    row.yes_ask = _decimal(market.get("yes_ask"))
    row.no_bid = _decimal(market.get("no_bid"))
    row.no_ask = _decimal(market.get("no_ask"))
    row.last_price = _decimal(market.get("last_price"))
    row.yes_mid = (
        ((row.yes_bid + row.yes_ask) / Decimal("2")).quantize(Decimal("0.0001"))
        if row.yes_bid is not None and row.yes_ask is not None
        else None
    )
    row.no_mid = (
        ((row.no_bid + row.no_ask) / Decimal("2")).quantize(Decimal("0.0001"))
        if row.no_bid is not None and row.no_ask is not None
        else None
    )
    row.raw_payload = market


def sync_kalshi_markets(session: Session, max_pages: int = 3, fetch_orderbooks: bool = True) -> int:
    client = KalshiClient.from_settings()
    close_start = utc_now() - timedelta(days=2)
    close_end = utc_now() + timedelta(days=21)
    params = {
        "limit": 200,
        "min_close_ts": int(close_start.timestamp()),
        "max_close_ts": int(close_end.timestamp()),
    }

    count = 0
    for market in client.iter_markets(params=params, max_pages=max_pages):
        ticker = str(market.get("ticker") or "").strip()
        if not ticker:
            continue

        status = _market_status(market)
        existing = session.scalar(select(KalshiMarket).where(KalshiMarket.ticker == ticker))
        should_track_new_market = status in DISCOVERABLE_MARKET_STATUSES and looks_like_baseball_market(market)
        if existing is None and not should_track_new_market:
            continue

        row = existing or KalshiMarket(ticker=ticker, kalshi_market_id=str(market.get("id") or ticker))
        _update_market_fields(row, market, ticker, status)

        if status not in DISCOVERABLE_MARKET_STATUSES:
            _clear_orderbook_prices(row)
            row.orderbook_raw = {"skipped": f"market status {row.status}"}
            session.add(row)
            count += 1
            continue

        if fetch_orderbooks:
            try:
                orderbook_payload = client.get_orderbook(ticker)
                orderbook = orderbook_payload.get("orderbook") or orderbook_payload
                derived = derive_orderbook_prices(orderbook)
                row.best_yes_bid = derived["best_yes_bid"]
                row.best_no_bid = derived["best_no_bid"]
                row.implied_yes_ask = derived["implied_yes_ask"]
                row.implied_no_ask = derived["implied_no_ask"]
                row.orderbook_raw = orderbook_payload
            except Exception as exc:
                _clear_orderbook_prices(row)
                row.orderbook_raw = {"error": str(exc)}

        session.add(row)
        count += 1

    session.commit()
    return count
