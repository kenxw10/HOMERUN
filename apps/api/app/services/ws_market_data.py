from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import KalshiMarket, MarketDataWorkerStatus, ModelCandidate, PaperTrade
from app.services.paper_epoch import get_or_create_active_paper_epoch
from app.time_utils import utc_now

STATUS_KEY = "kalshi_ws_paper"


def get_or_create_ws_status(session: Session) -> MarketDataWorkerStatus:
    row = session.scalar(select(MarketDataWorkerStatus).where(MarketDataWorkerStatus.status_key == STATUS_KEY))
    if row is not None:
        return row
    settings = get_settings()
    row = MarketDataWorkerStatus(
        status_key=STATUS_KEY,
        enabled=settings.websocket_market_data_enabled,
        running=False,
        source="websocket" if settings.websocket_market_data_enabled else "rest_fallback",
        subscribed_market_count=0,
        reconnect_count=0,
        stale_count=0,
        raw_status={},
    )
    session.add(row)
    session.flush()
    return row


def active_ws_tickers(session: Session) -> list[str]:
    settings = get_settings()
    epoch = get_or_create_active_paper_epoch(session)
    tickers: list[str] = []
    if settings.ws_subscribe_open_positions:
        tickers.extend(
            ticker
            for ticker in session.scalars(
                select(PaperTrade.market_ticker)
                .where(PaperTrade.paper_trading_epoch_id == epoch.id)
                .where(PaperTrade.status == "open")
            )
            if ticker
        )
    if settings.ws_subscribe_active_candidates:
        rows = list(
            session.execute(
                select(ModelCandidate, KalshiMarket)
                .join(KalshiMarket, ModelCandidate.kalshi_market_id == KalshiMarket.id)
                .where(ModelCandidate.paper_trading_epoch_id == epoch.id)
                .where(ModelCandidate.decision.in_(["eligible_for_paper_trade", "paper_trade", "candidate_only"]))
                .order_by(ModelCandidate.evaluated_at.desc())
                .limit(settings.ws_max_markets)
            )
        )
        tickers.extend(market.ticker for _candidate, market in rows if market.ticker)
    deduped: list[str] = []
    seen: set[str] = set()
    for ticker in tickers:
        normalized = ticker.upper()
        if normalized not in seen:
            deduped.append(normalized)
            seen.add(normalized)
        if len(deduped) >= settings.ws_max_markets:
            break
    return deduped


def mark_ws_status(
    session: Session,
    *,
    running: bool,
    subscribed_market_count: int = 0,
    last_message: bool = False,
    error: str | None = None,
    reconnect_increment: bool = False,
    source: str | None = None,
) -> MarketDataWorkerStatus:
    settings = get_settings()
    now = utc_now()
    row = get_or_create_ws_status(session)
    row.enabled = settings.websocket_market_data_enabled
    row.running = running
    row.source = source or ("websocket" if settings.websocket_market_data_enabled and running else "rest_fallback")
    row.subscribed_market_count = subscribed_market_count
    row.last_seen_at = now
    row.heartbeat_at = now
    if last_message:
        row.last_message_at = now
    if reconnect_increment:
        row.reconnect_count += 1
    if error is not None:
        row.last_error = error
    timeout = timedelta(seconds=settings.ws_price_stale_after_seconds)
    stale_cutoff = now - timeout
    if row.last_message_at is None or row.last_message_at < stale_cutoff:
        row.stale_count = subscribed_market_count if subscribed_market_count else row.stale_count
    row.raw_status = {
        "heartbeat_at": now.isoformat(),
        "ws_price_stale_after_seconds": settings.ws_price_stale_after_seconds,
        "ws_heartbeat_timeout_seconds": settings.ws_heartbeat_timeout_seconds,
    }
    session.add(row)
    session.flush()
    return row


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.0001"))
    except Exception:
        return None


LEGACY_CENT_PRICE_KEYS = {"yes_bid", "yes_ask", "no_bid", "no_ask", "last_price"}
PRICE_FIELD_KEYS = {
    "yes_bid": ("yes_bid_dollars", "yes_bid"),
    "yes_ask": ("yes_ask_dollars", "yes_ask"),
    "no_bid": ("no_bid_dollars", "no_bid"),
    "no_ask": ("no_ask_dollars", "no_ask"),
    "last_price": ("last_price_dollars", "last_price"),
    "best_yes_bid": ("best_yes_bid", "yes_bid_dollars", "yes_bid"),
    "best_no_bid": ("best_no_bid", "no_bid_dollars", "no_bid"),
    "implied_yes_ask": ("implied_yes_ask", "yes_ask_dollars", "yes_ask"),
    "implied_no_ask": ("implied_no_ask", "no_ask_dollars", "no_ask"),
}
EXECUTABLE_YES_PRICE_ATTRS = {"yes_ask", "implied_yes_ask", "no_bid", "best_no_bid"}


def _payload_price(payload: dict[str, Any], keys: tuple[str, ...]) -> Decimal | None:
    for key in keys:
        if payload.get(key) is None:
            continue
        value = _decimal(payload.get(key))
        if value is None:
            continue
        if key in LEGACY_CENT_PRICE_KEYS:
            return (value / Decimal("100")).quantize(Decimal("0.0001"))
        return value
    return None


def apply_ws_market_update(session: Session, ticker: str, payload: dict[str, Any]) -> dict[str, object]:
    now = utc_now()
    market = session.scalar(select(KalshiMarket).where(KalshiMarket.ticker == ticker).limit(1))
    if market is None:
        return {"updated": False, "reason": "unknown_market", "ticker": ticker}

    price_applied = False
    executable_price_applied = False
    for attr, keys in PRICE_FIELD_KEYS.items():
        value = _payload_price(payload, keys)
        if value is not None:
            setattr(market, attr, value)
            price_applied = True
            if attr in EXECUTABLE_YES_PRICE_ATTRS:
                executable_price_applied = True
    market.websocket_updated_at = now
    if price_applied:
        market.market_data_source = "websocket"
    if executable_price_applied:
        market.market_price_updated_at = now
    session.add(market)

    epoch = get_or_create_active_paper_epoch(session)
    updated_trades = 0
    if price_applied:
        for trade in session.scalars(
            select(PaperTrade)
            .where(PaperTrade.paper_trading_epoch_id == epoch.id)
            .where(PaperTrade.market_ticker == ticker)
            .where(PaperTrade.status == "open")
        ):
            if trade.contract_side == "no":
                mark = market.best_no_bid or market.no_bid or ((Decimal("1.0000") - market.last_price) if market.last_price else None)
            else:
                mark = market.best_yes_bid or market.yes_bid or market.last_price
            if mark is None:
                continue
            trade.current_price = mark
            trade.current_price_updated_at = now
            session.add(trade)
            updated_trades += 1
    mark_ws_status(session, running=True, subscribed_market_count=len(active_ws_tickers(session)), last_message=True, source="websocket")
    return {"updated": price_applied, "ticker": ticker, "updated_trades": updated_trades}


def ws_status_payload(session: Session) -> dict[str, object]:
    row = get_or_create_ws_status(session)
    return {
        "enabled": row.enabled,
        "running": row.running,
        "last_seen": row.last_seen_at.isoformat() if row.last_seen_at else None,
        "last_message_at": row.last_message_at.isoformat() if row.last_message_at else None,
        "subscribed_market_count": row.subscribed_market_count,
        "reconnect_count": row.reconnect_count,
        "last_error": row.last_error,
        "stale_count": row.stale_count,
        "source": row.source,
    }
