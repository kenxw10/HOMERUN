from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.config import get_settings
from app.services.http_json import get_json


def _as_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except Exception:
        return None
    if parsed > 1:
        parsed = parsed / Decimal("100")
    return parsed.quantize(Decimal("0.0001"))


def _bid_price(level: object) -> Decimal | None:
    if isinstance(level, dict):
        for key in ("price", "yes_bid", "no_bid"):
            if key in level:
                return _as_decimal(level[key])
    if isinstance(level, (list, tuple)) and level:
        return _as_decimal(level[0])
    return _as_decimal(level)


def _best_bid(levels: object) -> Decimal | None:
    if not isinstance(levels, list):
        return None
    prices = [price for price in (_bid_price(level) for level in levels) if price is not None]
    return max(prices) if prices else None


def _unwrap_orderbook(orderbook: dict[str, Any]) -> dict[str, Any]:
    for key in ("orderbook", "orderbook_fp"):
        nested = orderbook.get(key)
        if isinstance(nested, dict):
            return nested
    return orderbook


def derive_orderbook_prices(orderbook: dict[str, Any]) -> dict[str, Decimal | None]:
    unwrapped = _unwrap_orderbook(orderbook)
    yes_levels = (
        unwrapped.get("yes")
        or unwrapped.get("yes_bids")
        or unwrapped.get("yesBid")
        or unwrapped.get("yes_dollars")
    )
    no_levels = (
        unwrapped.get("no")
        or unwrapped.get("no_bids")
        or unwrapped.get("noBid")
        or unwrapped.get("no_dollars")
    )
    best_yes_bid = _best_bid(yes_levels)
    best_no_bid = _best_bid(no_levels)

    return {
        "best_yes_bid": best_yes_bid,
        "best_no_bid": best_no_bid,
        "implied_yes_ask": (Decimal("1") - best_no_bid).quantize(Decimal("0.0001")) if best_no_bid is not None else None,
        "implied_no_ask": (Decimal("1") - best_yes_bid).quantize(Decimal("0.0001")) if best_yes_bid is not None else None,
    }


@dataclass
class KalshiClient:
    base_url: str
    api_key: str | None = None
    api_secret: str | None = None
    timeout_seconds: int = 15

    @classmethod
    def from_settings(cls) -> "KalshiClient":
        settings = get_settings()
        return cls(
            base_url=settings.kalshi_rest_base_url.rstrip("/"),
            api_key=settings.kalshi_api_key.get_secret_value() if settings.kalshi_api_key else None,
            api_secret=settings.kalshi_api_secret.get_secret_value() if settings.kalshi_api_secret else None,
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["KALSHI-ACCESS-KEY"] = self.api_key
        return headers

    def get_markets(self, params: dict[str, object] | None = None) -> dict[str, Any]:
        return get_json(
            f"{self.base_url}/markets",
            params=params or {},
            headers=self._headers(),
            timeout=self.timeout_seconds,
        )

    def iter_markets(self, params: dict[str, object] | None = None, max_pages: int = 3):
        query = dict(params or {})
        for _ in range(max_pages):
            payload = self.get_markets(query)
            for market in payload.get("markets") or payload.get("data") or []:
                yield market
            cursor = payload.get("cursor") or payload.get("next_cursor")
            if not cursor:
                break
            query["cursor"] = cursor

    def get_orderbook(self, ticker: str) -> dict[str, Any]:
        return get_json(
            f"{self.base_url}/markets/{ticker}/orderbook",
            headers=self._headers(),
            timeout=self.timeout_seconds,
        )
