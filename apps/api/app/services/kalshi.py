from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from email.utils import parsedate_to_datetime
import random
import time
from typing import Any

from app.config import get_settings
from app.services.http_json import HttpJsonError, get_json


def _as_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except Exception:
        return None
    return parsed.quantize(Decimal("0.0001"))


def _as_cent_decimal(value: object) -> Decimal | None:
    parsed = _as_decimal(value)
    return (parsed / Decimal("100")).quantize(Decimal("0.0001")) if parsed is not None else None


def _bid_price(level: object, *, dollars: bool) -> Decimal | None:
    parse = _as_decimal if dollars else _as_cent_decimal
    if isinstance(level, dict):
        keys = ("price_dollars", "yes_bid_dollars", "no_bid_dollars", "price") if dollars else ("price", "yes_bid", "no_bid")
        for key in keys:
            if key in level:
                return parse(level[key])
    if isinstance(level, (list, tuple)) and level:
        return parse(level[0])
    return parse(level)


def _best_bid(levels: object, *, dollars: bool) -> Decimal | None:
    if not isinstance(levels, list):
        return None
    prices = [price for price in (_bid_price(level, dollars=dollars) for level in levels) if price is not None]
    return max(prices) if prices else None


def _unwrap_orderbook(orderbook: dict[str, Any]) -> dict[str, Any]:
    for key in ("orderbook", "orderbook_fp"):
        nested = orderbook.get(key)
        if isinstance(nested, dict):
            return nested
    return orderbook


def derive_orderbook_prices(orderbook: dict[str, Any]) -> dict[str, Decimal | None]:
    unwrapped = _unwrap_orderbook(orderbook)
    yes_dollars = "yes_dollars" in unwrapped
    no_dollars = "no_dollars" in unwrapped
    yes_levels = unwrapped.get("yes_dollars") if yes_dollars else (unwrapped.get("yes") or unwrapped.get("yes_bids") or unwrapped.get("yesBid"))
    no_levels = unwrapped.get("no_dollars") if no_dollars else (unwrapped.get("no") or unwrapped.get("no_bids") or unwrapped.get("noBid"))
    best_yes_bid = _best_bid(yes_levels, dollars=yes_dollars)
    best_no_bid = _best_bid(no_levels, dollars=no_dollars)

    return {
        "best_yes_bid": best_yes_bid,
        "best_no_bid": best_no_bid,
        "implied_yes_ask": (Decimal("1") - best_no_bid).quantize(Decimal("0.0001")) if best_no_bid is not None else None,
        "implied_no_ask": (Decimal("1") - best_yes_bid).quantize(Decimal("0.0001")) if best_yes_bid is not None else None,
    }


class KalshiAPIError(RuntimeError):
    def __init__(self, message: str, *, source: HttpJsonError, retry_or_fallback_attempted: bool = False) -> None:
        super().__init__(message)
        self.source = source
        self.retry_or_fallback_attempted = retry_or_fallback_attempted

    def to_detail(self) -> dict[str, object]:
        detail = self.source.to_detail()
        detail["retry_or_fallback_attempted"] = self.retry_or_fallback_attempted
        return detail


@dataclass
class KalshiClient:
    base_url: str
    api_key: str | None = None
    api_secret: str | None = None
    timeout_seconds: int = 15
    min_request_interval_ms: int = 0
    max_retries: int = 0
    backoff_base_ms: int = 0
    backoff_max_ms: int = 0
    request_count: int = field(default=0, init=False)
    rate_limited_count: int = field(default=0, init=False)
    retries_attempted: int = field(default=0, init=False)
    _last_request_at: float = field(default=0.0, init=False, repr=False)

    @classmethod
    def from_settings(cls) -> "KalshiClient":
        settings = get_settings()
        return cls(
            base_url=settings.kalshi_rest_base_url.rstrip("/"),
            api_key=settings.kalshi_api_key.get_secret_value() if settings.kalshi_api_key else None,
            api_secret=settings.kalshi_api_secret.get_secret_value() if settings.kalshi_api_secret else None,
        )

    @classmethod
    def from_market_data_settings(cls) -> "KalshiClient":
        settings = get_settings()
        return cls(
            base_url=settings.kalshi_market_data_base_url.rstrip("/"),
            min_request_interval_ms=settings.kalshi_market_data_min_request_interval_ms,
            max_retries=settings.kalshi_market_data_max_retries,
            backoff_base_ms=settings.kalshi_market_data_backoff_base_ms,
            backoff_max_ms=settings.kalshi_market_data_backoff_max_ms,
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["KALSHI-ACCESS-KEY"] = self.api_key
        return headers

    def _retry_after_seconds(self, exc: HttpJsonError) -> float | None:
        value = exc.response_headers.get("Retry-After") or exc.response_headers.get("retry-after")
        if not value:
            return None
        try:
            return max(float(value), 0.0)
        except ValueError:
            pass
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        return max(retry_at.timestamp() - time.time(), 0.0)

    def _backoff_seconds(self, attempt: int, exc: HttpJsonError) -> float:
        retry_after = self._retry_after_seconds(exc)
        if retry_after is not None:
            return retry_after
        base = max(self.backoff_base_ms, 0) / 1000
        maximum = max(self.backoff_max_ms, self.backoff_base_ms, 0) / 1000
        if base <= 0:
            return 0.0
        return min(base * (2**attempt), maximum) + random.uniform(0, min(base, 0.25))

    def _throttle(self) -> None:
        interval = max(self.min_request_interval_ms, 0) / 1000
        if interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        if self._last_request_at > 0 and elapsed < interval:
            time.sleep(interval - elapsed)

    def _raw_get_json(self, path: str, params: dict[str, object] | None = None) -> dict[str, Any]:
        endpoint = f"{self.base_url}{path}"
        self._throttle()
        self.request_count += 1
        self._last_request_at = time.monotonic()
        return get_json(
            endpoint,
            params=params or {},
            headers=self._headers(),
            timeout=self.timeout_seconds,
        )

    def _get_json(self, path: str, params: dict[str, object] | None = None) -> dict[str, Any]:
        for attempt in range(self.max_retries + 1):
            try:
                return self._raw_get_json(path, params=params)
            except HttpJsonError as exc:
                if exc.status_code == 429:
                    self.rate_limited_count += 1
                if exc.status_code != 429 or attempt >= self.max_retries:
                    raise KalshiAPIError(
                        f"Kalshi GET {path} failed.",
                        source=exc,
                        retry_or_fallback_attempted=attempt > 0,
                    ) from exc

                self.retries_attempted += 1
                time.sleep(self._backoff_seconds(attempt, exc))

        raise RuntimeError("unreachable Kalshi retry state")

    def get_markets(self, params: dict[str, object] | None = None) -> dict[str, Any]:
        return self._get_json("/markets", params=params or {})

    def get_market(self, ticker: str) -> dict[str, Any]:
        return self._get_json(f"/markets/{ticker}")

    def get_markets_by_tickers(self, tickers: list[str]) -> dict[str, Any]:
        return self.get_markets({"tickers": ",".join(tickers), "limit": len(tickers), "mve_filter": "exclude"})

    def get_markets_by_event_ticker(self, event_ticker: str, limit: int = 100) -> dict[str, Any]:
        return self.get_markets({"event_ticker": event_ticker, "limit": limit, "mve_filter": "exclude"})

    def get_markets_by_series_window(
        self,
        series_ticker: str,
        min_close_ts: int,
        max_close_ts: int,
        *,
        limit: int = 100,
        max_pages: int = 2,
    ) -> list[dict[str, Any]]:
        return list(
            self.iter_markets(
                params={
                    "series_ticker": series_ticker,
                    "min_close_ts": min_close_ts,
                    "max_close_ts": max_close_ts,
                    "limit": limit,
                    "mve_filter": "exclude",
                },
                max_pages=max_pages,
            )
        )

    def get_event(self, event_ticker: str) -> dict[str, Any]:
        return self._get_json(f"/events/{event_ticker}")

    def iter_markets(self, params: dict[str, object] | None = None, max_pages: int | None = None):
        query = dict(params or {})
        pages_seen = 0
        cursors_seen: set[str] = set()
        while max_pages is None or pages_seen < max_pages:
            payload = self.get_markets(query)
            pages_seen += 1
            for market in payload.get("markets") or payload.get("data") or []:
                yield market
            cursor = payload.get("cursor") or payload.get("next_cursor")
            if not cursor or cursor in cursors_seen:
                break
            cursors_seen.add(cursor)
            query["cursor"] = cursor

    def get_orderbook(self, ticker: str) -> dict[str, Any]:
        return self._get_json(f"/markets/{ticker}/orderbook")
