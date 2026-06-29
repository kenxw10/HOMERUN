from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import KalshiMarket, MarketDataWorkerStatus, ModelCandidate, PaperTrade
from app.services.kalshi import derive_orderbook_prices
from app.services.paper_epoch import get_or_create_active_paper_epoch
from app.time_utils import ensure_aware_utc, utc_now

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
        row.stale_count = 0
    if reconnect_increment:
        row.reconnect_count += 1
    if error is not None:
        row.last_error = error
    timeout = timedelta(seconds=settings.ws_price_stale_after_seconds)
    stale_cutoff = now - timeout
    last_message_at = ensure_aware_utc(row.last_message_at) if row.last_message_at is not None else None
    if last_message_at is None or last_message_at < stale_cutoff:
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
EXECUTABLE_NO_PRICE_ATTRS = {"no_ask", "implied_no_ask", "yes_bid", "best_yes_bid"}
EXECUTABLE_PRICE_ATTRS = EXECUTABLE_YES_PRICE_ATTRS | EXECUTABLE_NO_PRICE_ATTRS
POSITION_MARK_PRICE_ATTRS = {"yes_bid", "best_yes_bid", "no_bid", "best_no_bid", "last_price"}
INVERSE_YES_PRICE_ATTRS = {"implied_yes_ask", "no_bid", "best_no_bid"}
INVERSE_NO_PRICE_ATTRS = {"implied_no_ask", "yes_bid", "best_yes_bid"}
YES_POSITION_MARK_ATTRS = {"yes_bid", "best_yes_bid"}
NO_POSITION_MARK_ATTRS = {"no_bid", "best_no_bid"}
ORDERBOOK_SNAPSHOT_LEVEL_KEYS = {
    "yes_dollars_fp": "yes_dollars",
    "no_dollars_fp": "no_dollars",
    "yes_dollars": "yes_dollars",
    "no_dollars": "no_dollars",
    "yes": "yes",
    "no": "no",
    "yes_bids": "yes_bids",
    "no_bids": "no_bids",
}
WS_ORDERBOOK_RAW_KEY = "websocket_orderbook"
YES_BOOK_KEYS = ("yes_dollars", "yes", "yes_bids")
NO_BOOK_KEYS = ("no_dollars", "no", "no_bids")
DOLLAR_BOOK_KEYS = {"yes_dollars", "no_dollars"}


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


def _first_present(mapping: dict[str, Any], *keys: str) -> object | None:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _book_price_key(side: str, book: dict[str, dict[str, str]]) -> str:
    preferred = f"{side}_dollars"
    fallback_keys = YES_BOOK_KEYS if side == "yes" else NO_BOOK_KEYS
    if preferred in book or not any(key in book for key in fallback_keys):
        return preferred
    return next(key for key in fallback_keys if key in book)


def _book_levels_for_derive(book: dict[str, dict[str, str]]) -> dict[str, list[list[str]]]:
    orderbook: dict[str, list[list[str]]] = {}
    for key, levels_by_price in book.items():
        if not isinstance(levels_by_price, dict):
            continue
        levels: list[list[str]] = []
        for price, quantity in levels_by_price.items():
            parsed_quantity = _decimal(quantity)
            if parsed_quantity is not None and parsed_quantity > 0:
                levels.append([price, str(parsed_quantity)])
        if levels:
            levels.sort(key=lambda level: Decimal(level[0]), reverse=True)
            orderbook[key] = levels
    return orderbook


def _ws_orderbook(market: KalshiMarket) -> dict[str, dict[str, str]]:
    raw = market.orderbook_raw if isinstance(market.orderbook_raw, dict) else {}
    raw_book = raw.get(WS_ORDERBOOK_RAW_KEY)
    if not isinstance(raw_book, dict):
        return {}
    book: dict[str, dict[str, str]] = {}
    for key, levels_by_price in raw_book.items():
        if not isinstance(key, str) or not isinstance(levels_by_price, dict):
            continue
        book[key] = {str(price): str(quantity) for price, quantity in levels_by_price.items()}
    return book


def _store_ws_orderbook(market: KalshiMarket, book: dict[str, dict[str, str]]) -> None:
    raw = dict(market.orderbook_raw or {})
    raw[WS_ORDERBOOK_RAW_KEY] = {key: dict(levels) for key, levels in book.items()}
    market.orderbook_raw = raw


def _snapshot_book_side(levels: object) -> dict[str, str]:
    if not isinstance(levels, list):
        return {}
    parsed_levels: dict[str, str] = {}
    for level in levels:
        price_value: object | None = None
        quantity_value: object | None = None
        if isinstance(level, dict):
            price_value = _first_present(level, "price_dollars", "price")
            quantity_value = _first_present(level, "quantity", "qty", "count")
        elif isinstance(level, (list, tuple)) and level:
            price_value = level[0]
            quantity_value = level[1] if len(level) > 1 else Decimal("1")
        else:
            price_value = level
            quantity_value = Decimal("1")
        price = _decimal(price_value)
        quantity = _decimal(quantity_value)
        if price is None or quantity is None or quantity <= 0:
            continue
        parsed_levels[str(price)] = str(quantity)
    return parsed_levels


def _derived_book_updates(book: dict[str, dict[str, str]], sides: set[str]) -> dict[str, Decimal | None]:
    derived = derive_orderbook_prices(_book_levels_for_derive(book))
    updates: dict[str, Decimal | None] = {}
    if "yes" in sides:
        updates["best_yes_bid"] = derived["best_yes_bid"]
        updates["implied_no_ask"] = derived["implied_no_ask"]
    if "no" in sides:
        updates["best_no_bid"] = derived["best_no_bid"]
        updates["implied_yes_ask"] = derived["implied_yes_ask"]
    return updates


def _legacy_delta_clear(market: KalshiMarket, side: str, price: Decimal, delta: Decimal) -> dict[str, Decimal | None]:
    if side == "yes" and delta < 0 and market.best_yes_bid is not None and price >= market.best_yes_bid:
        return {"best_yes_bid": None, "implied_no_ask": None}
    if side == "no" and delta < 0 and market.best_no_bid is not None and price >= market.best_no_bid:
        return {"best_no_bid": None, "implied_yes_ask": None}
    return {}


def _mark_sides_for_attr(attr: str, value: Decimal | None) -> set[str]:
    if value is None:
        return set()
    if attr in YES_POSITION_MARK_ATTRS:
        return {"yes"}
    if attr in NO_POSITION_MARK_ATTRS:
        return {"no"}
    if attr == "last_price":
        return {"yes", "no"}
    return set()


def _clear_stale_yes_ask_fields(market: KalshiMarket, executable_attrs: set[str]) -> None:
    if not executable_attrs or "yes_ask" in executable_attrs:
        return
    market.yes_ask = None
    if "implied_yes_ask" not in executable_attrs:
        market.implied_yes_ask = None


def _clear_stale_no_ask_fields(market: KalshiMarket, executable_attrs: set[str]) -> None:
    if not executable_attrs or "no_ask" in executable_attrs:
        return
    market.no_ask = None
    if "implied_no_ask" not in executable_attrs:
        market.implied_no_ask = None


def _orderbook_snapshot_prices(market: KalshiMarket, payload: dict[str, Any]) -> dict[str, Decimal | None]:
    orderbook = {
        target_key: payload[source_key]
        for source_key, target_key in ORDERBOOK_SNAPSHOT_LEVEL_KEYS.items()
        if source_key in payload
    }
    if not orderbook:
        return {}
    book = _ws_orderbook(market)
    sides: set[str] = set()
    for key, levels in orderbook.items():
        book[key] = _snapshot_book_side(levels)
        if key.startswith("yes"):
            sides.add("yes")
        if key.startswith("no"):
            sides.add("no")
    _store_ws_orderbook(market, book)
    return _derived_book_updates(book, sides)


def _delta_value(payload: dict[str, Any]) -> Decimal | None:
    for key in ("delta_fp", "delta", "quantity_delta"):
        value = _decimal(payload.get(key))
        if value is not None:
            return value
    return None


def _orderbook_delta_price(payload: dict[str, Any]) -> Decimal | None:
    value = _decimal(payload.get("price_dollars"))
    if value is not None:
        return value
    value = _decimal(payload.get("price"))
    return (value / Decimal("100")).quantize(Decimal("0.0001")) if value is not None else None


def _orderbook_delta_storage_price(payload: dict[str, Any], *, dollars: bool) -> Decimal | None:
    dollar_value = _decimal(payload.get("price_dollars"))
    cent_value = _decimal(payload.get("price"))
    if dollars:
        if dollar_value is not None:
            return dollar_value
        return (cent_value / Decimal("100")).quantize(Decimal("0.0001")) if cent_value is not None else None
    if cent_value is not None:
        return cent_value
    return (dollar_value * Decimal("100")).quantize(Decimal("0.0001")) if dollar_value is not None else None


def _orderbook_delta_prices(market: KalshiMarket, payload: dict[str, Any]) -> dict[str, Decimal | None]:
    side = str(payload.get("side") or "").strip().lower()
    price = _orderbook_delta_price(payload)
    delta = _delta_value(payload)
    if side not in {"yes", "no"} or price is None or delta is None:
        return {}

    book = _ws_orderbook(market)
    book_key = _book_price_key(side, book)
    storage_price = _orderbook_delta_storage_price(payload, dollars=book_key in DOLLAR_BOOK_KEYS)
    if storage_price is None:
        return {}
    storage_key = str(storage_price)
    levels = dict(book.get(book_key) or {})
    if book_key not in book and delta > 0:
        current_best = market.best_yes_bid if side == "yes" else market.best_no_bid
        if current_best is not None and price < current_best:
            return {}
    existing_quantity = _decimal(levels.get(storage_key)) or Decimal("0")
    if existing_quantity == 0 and delta < 0:
        return _legacy_delta_clear(market, side, price, delta)

    new_quantity = existing_quantity + delta
    if new_quantity > 0:
        levels[storage_key] = str(new_quantity.quantize(Decimal("0.0001")))
    else:
        levels.pop(storage_key, None)
    book[book_key] = levels
    _store_ws_orderbook(market, book)
    return _derived_book_updates(book, {side})


def apply_ws_market_update(session: Session, ticker: str, payload: dict[str, Any]) -> dict[str, object]:
    now = utc_now()
    market = session.scalar(select(KalshiMarket).where(KalshiMarket.ticker == ticker).limit(1))
    if market is None:
        return {"updated": False, "reason": "unknown_market", "ticker": ticker}

    price_applied = False
    executable_attrs_applied: set[str] = set()
    mark_sides_applied: set[str] = set()
    for attr, keys in PRICE_FIELD_KEYS.items():
        value = _payload_price(payload, keys)
        if value is not None:
            setattr(market, attr, value)
            price_applied = True
            if attr in EXECUTABLE_PRICE_ATTRS:
                executable_attrs_applied.add(attr)
            mark_sides_applied.update(_mark_sides_for_attr(attr, value))
    for attr, value in {
        **_orderbook_snapshot_prices(market, payload),
        **_orderbook_delta_prices(market, payload),
    }.items():
        setattr(market, attr, value)
        price_applied = True
        if value is not None and attr in EXECUTABLE_PRICE_ATTRS:
            executable_attrs_applied.add(attr)
        mark_sides_applied.update(_mark_sides_for_attr(attr, value))
    _clear_stale_yes_ask_fields(market, executable_attrs_applied)
    _clear_stale_no_ask_fields(market, executable_attrs_applied)
    market.websocket_updated_at = now
    if price_applied:
        market.market_data_source = "websocket"
    if executable_attrs_applied:
        market.market_price_updated_at = now
    session.add(market)

    epoch = get_or_create_active_paper_epoch(session)
    updated_trades = 0
    if mark_sides_applied:
        for trade in session.scalars(
            select(PaperTrade)
            .where(PaperTrade.paper_trading_epoch_id == epoch.id)
            .where(PaperTrade.market_ticker == ticker)
            .where(PaperTrade.status == "open")
        ):
            if trade.contract_side not in mark_sides_applied:
                continue
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
    mark_ws_status(
        session,
        running=True,
        subscribed_market_count=len(active_ws_tickers(session)),
        last_message=price_applied,
        source="websocket",
    )
    return {"updated": price_applied, "ticker": ticker, "updated_trades": updated_trades}


def ws_status_running_is_fresh(row: MarketDataWorkerStatus, *, now=None) -> bool:
    if not row.enabled or not row.running:
        return False
    newest_seen = max(
        (ensure_aware_utc(value) for value in (row.heartbeat_at, row.last_seen_at) if value is not None),
        default=None,
    )
    if newest_seen is None:
        return False
    timeout = timedelta(seconds=get_settings().ws_heartbeat_timeout_seconds)
    return newest_seen >= ensure_aware_utc(now or utc_now()) - timeout


def ws_status_payload(session: Session) -> dict[str, object]:
    row = get_or_create_ws_status(session)
    running = ws_status_running_is_fresh(row)
    return {
        "enabled": row.enabled,
        "running": running,
        "last_seen": row.last_seen_at.isoformat() if row.last_seen_at else None,
        "last_message_at": row.last_message_at.isoformat() if row.last_message_at else None,
        "subscribed_market_count": row.subscribed_market_count,
        "reconnect_count": row.reconnect_count,
        "last_error": row.last_error,
        "stale_count": row.stale_count,
        "source": row.source if running else "rest_fallback",
    }
