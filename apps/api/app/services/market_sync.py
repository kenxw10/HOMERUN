from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import KalshiMarket, MarketMapping, MlbGame
from app.services.kalshi import KalshiAPIError, KalshiClient, derive_orderbook_prices
from app.services.kalshi_mlb_resolver import (
    TARGETED_SERIES_TICKER,
    GameResolution,
    ResolvedMarket,
    resolve_game_markets,
)
from app.time_utils import ensure_aware_utc, get_dashboard_zone, parse_datetime, utc_now

logger = logging.getLogger(__name__)

DISCOVERY_LOOKAHEAD = timedelta(days=21)
TRADABLE_MARKET_STATUSES = {"active", "open"}


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except Exception:
        return None
    return parsed.quantize(Decimal("0.0001"))


def _cent_decimal(value: object) -> Decimal | None:
    parsed = _decimal(value)
    return (parsed / Decimal("100")).quantize(Decimal("0.0001")) if parsed is not None else None


def _market_decimal(market: dict[str, Any], dollars_key: str, legacy_key: str) -> Decimal | None:
    value = _decimal(market.get(dollars_key))
    return value if value is not None else _cent_decimal(market.get(legacy_key))


def _market_status(market: dict[str, Any]) -> str:
    return str(market.get("status") or "").strip().lower()


def _stored_market_status(raw_status: str) -> str:
    if raw_status in TRADABLE_MARKET_STATUSES:
        return "open"
    return raw_status or "untracked"


def _clear_orderbook_prices(row: KalshiMarket) -> None:
    row.best_yes_bid = None
    row.best_no_bid = None
    row.implied_yes_ask = None
    row.implied_no_ask = None


def _update_market_fields(row: KalshiMarket, market: dict[str, Any], ticker: str, raw_status: str) -> None:
    observed_at = utc_now()
    row.kalshi_market_id = str(market.get("id") or market.get("market_id") or ticker)
    row.event_ticker = market.get("event_ticker")
    row.title = market.get("title") or ticker
    row.subtitle = market.get("subtitle")
    row.rules = market.get("rules_primary") or market.get("rules")
    row.yes_subtitle = market.get("yes_sub_title") or market.get("yes_subtitle")
    row.no_subtitle = market.get("no_sub_title") or market.get("no_subtitle")
    row.raw_status = raw_status or None
    row.status = _stored_market_status(raw_status)
    row.open_time = parse_datetime(market.get("open_time"))
    row.close_time = parse_datetime(market.get("close_time"))
    row.occurrence_datetime = parse_datetime(market.get("occurrence_datetime") or market.get("expected_expiration_time"))
    row.resolve_time = parse_datetime(market.get("expiration_time") or market.get("resolve_time"))
    row.yes_bid = _market_decimal(market, "yes_bid_dollars", "yes_bid")
    row.yes_ask = _market_decimal(market, "yes_ask_dollars", "yes_ask")
    row.no_bid = _market_decimal(market, "no_bid_dollars", "no_bid")
    row.no_ask = _market_decimal(market, "no_ask_dollars", "no_ask")
    row.last_price = _market_decimal(market, "last_price_dollars", "last_price")
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
    row.market_price_updated_at = observed_at
    row.raw_payload = market


def _target_games(session: Session) -> list[MlbGame]:
    now = utc_now()
    return list(
        session.scalars(
            select(MlbGame)
            .where(MlbGame.scheduled_start >= now - timedelta(hours=2))
            .where(MlbGame.scheduled_start < now + DISCOVERY_LOOKAHEAD)
            .order_by(MlbGame.scheduled_start.asc())
        )
    )


def games_for_eastern_date(session: Session, target_date: date) -> list[MlbGame]:
    local_start = datetime.combine(target_date, time.min, tzinfo=get_dashboard_zone())
    start = ensure_aware_utc(local_start)
    end = start + timedelta(days=1)
    return list(
        session.scalars(
            select(MlbGame)
            .where(MlbGame.scheduled_start >= start)
            .where(MlbGame.scheduled_start < end)
            .order_by(MlbGame.scheduled_start.asc())
        )
    )


def _empty_summary() -> dict[str, object]:
    return {
        "games_considered": 0,
        "attempted_event_tickers": 0,
        "attempted_market_tickers": 0,
        "attempted_event_ticker_values": [],
        "attempted_market_ticker_values": [],
        "markets_upserted": 0,
        "mappings_created_or_updated": 0,
        "confirmed_mappings": 0,
        "candidate_mappings": 0,
        "needs_review_mappings": 0,
        "rejected_multivariate": 0,
        "errors": [],
        "games": [],
        "broad_discovery": {"enabled": False},
    }


def _append_unique(values: list[str], new_values: list[str]) -> None:
    seen = set(values)
    for value in new_values:
        if value not in seen:
            values.append(value)
            seen.add(value)


def _error_detail(exc: KalshiAPIError, *, fallback_attempted: bool) -> dict[str, object]:
    detail = exc.to_detail()
    detail["retry_or_fallback_attempted"] = fallback_attempted
    return detail


def _fetch_orderbook(client: KalshiClient, row: KalshiMarket) -> None:
    if row.status != "open":
        _clear_orderbook_prices(row)
        row.orderbook_raw = {"skipped": f"market status {row.status}"}
        return

    try:
        orderbook_payload = client.get_orderbook(row.ticker)
        derived = derive_orderbook_prices(orderbook_payload)
        row.best_yes_bid = derived["best_yes_bid"]
        row.best_no_bid = derived["best_no_bid"]
        row.implied_yes_ask = derived["implied_yes_ask"]
        row.implied_no_ask = derived["implied_no_ask"]
        row.market_price_updated_at = utc_now()
        row.orderbook_raw = orderbook_payload
    except KalshiAPIError as exc:
        _clear_orderbook_prices(row)
        row.orderbook_raw = {"error": _error_detail(exc, fallback_attempted=False)}
    except Exception as exc:
        _clear_orderbook_prices(row)
        row.orderbook_raw = {"error": str(exc)}


def _mapping_metadata(resolution: GameResolution, match: ResolvedMarket) -> dict[str, object]:
    metadata = dict(match.metadata)
    metadata.update(
        {
            "attempted_event_tickers": resolution.attempted_event_tickers,
            "attempted_market_tickers": resolution.attempted_market_tickers,
            "resolver_strategy_used": match.resolver_strategy,
        }
    )
    return metadata


def _upsert_mapping(
    session: Session,
    game: MlbGame,
    market: KalshiMarket,
    resolution: GameResolution,
    match: ResolvedMarket,
) -> None:
    mapping = session.scalar(
        select(MarketMapping)
        .where(MarketMapping.mlb_game_id == game.id)
        .where(MarketMapping.kalshi_market_id == market.id)
    )
    mapping = mapping or MarketMapping(mlb_game_id=game.id, kalshi_market_id=market.id)
    mapping.mapping_status = match.mapping_status
    mapping.confidence = match.confidence
    mapping.rationale = match.rationale
    mapping.resolver_strategy = match.resolver_strategy
    mapping.validation_status = match.validation_status
    mapping.market_family = "full_game_winner"
    mapping.market_type = "full_game_winner"
    match_ticker = str(match.market.get("ticker") or "")
    mapping.selection_code = match_ticker.rsplit("-", 1)[-1].upper() if "-" in match_ticker else None
    mapping.inning_scope = "full_game"
    mapping.settlement_rule_status = (
        "paper_supported" if match.validation_status == "confirmed_for_paper" else "needs_review"
    )
    mapping.mapping_metadata = _mapping_metadata(resolution, match)
    session.add(mapping)


def _record_mapping_count(summary: dict[str, object], mapping_status: str) -> None:
    if mapping_status == "confirmed":
        summary["confirmed_mappings"] = int(summary["confirmed_mappings"]) + 1
    elif mapping_status == "candidate":
        summary["candidate_mappings"] = int(summary["candidate_mappings"]) + 1
    elif mapping_status == "needs_review":
        summary["needs_review_mappings"] = int(summary["needs_review_mappings"]) + 1
    elif mapping_status == "rejected_multivariate":
        summary["rejected_multivariate"] = int(summary["rejected_multivariate"]) + 1


def _run_broad_discovery_diagnostic(client: KalshiClient, max_pages: int | None = None) -> dict[str, object]:
    settings = get_settings()
    pages = max_pages or settings.kalshi_market_sync_max_pages
    params = {
        "series_ticker": TARGETED_SERIES_TICKER,
        "limit": settings.kalshi_market_sync_limit,
        "mve_filter": "exclude",
    }
    try:
        markets = list(client.iter_markets(params=params, max_pages=pages))
    except KalshiAPIError as exc:
        return {
            "enabled": True,
            "ok": False,
            "params": params,
            "max_pages": pages,
            "error": _error_detail(exc, fallback_attempted=False),
        }
    return {
        "enabled": True,
        "ok": True,
        "params": params,
        "max_pages": pages,
        "market_count": len(markets),
        "sample_tickers": [str(market.get("ticker") or "") for market in markets[:10]],
    }


def sync_kalshi_markets(session: Session, max_pages: int | None = None, fetch_orderbooks: bool = True) -> dict[str, object]:
    client = KalshiClient.from_market_data_settings()
    settings = get_settings()
    summary = _empty_summary()

    for game in _target_games(session):
        summary["games_considered"] = int(summary["games_considered"]) + 1
        logger.info("Starting targeted Kalshi MLB resolver for game %s", game.external_game_id)
        try:
            resolution = resolve_game_markets(client, game)
        except Exception as exc:
            logger.exception("Targeted Kalshi resolver failed for game %s", game.external_game_id)
            errors = summary["errors"]
            assert isinstance(errors, list)
            errors.append({"game_id": game.external_game_id, "message": str(exc)})
            continue

        summary["attempted_event_tickers"] = int(summary["attempted_event_tickers"]) + len(resolution.attempted_event_tickers)
        summary["attempted_market_tickers"] = int(summary["attempted_market_tickers"]) + len(resolution.attempted_market_tickers)
        _append_unique(summary["attempted_event_ticker_values"], resolution.attempted_event_tickers)  # type: ignore[arg-type]
        _append_unique(summary["attempted_market_ticker_values"], resolution.attempted_market_tickers)  # type: ignore[arg-type]
        games = summary["games"]
        assert isinstance(games, list)
        games.append(resolution.to_preview_dict())
        errors = summary["errors"]
        assert isinstance(errors, list)
        errors.extend({"game_id": game.external_game_id, **error} for error in resolution.errors)

        for match in resolution.matches:
            ticker = str(match.market.get("ticker") or "").strip()
            if not ticker:
                continue
            raw_status = _market_status(match.market)
            market = session.scalar(select(KalshiMarket).where(KalshiMarket.ticker == ticker))
            market = market or KalshiMarket(ticker=ticker, kalshi_market_id=str(match.market.get("id") or ticker))
            _update_market_fields(market, match.market, ticker, raw_status)
            market.market_family = "full_game_winner"
            market.market_type = "full_game_winner"
            market.selection_code = ticker.rsplit("-", 1)[-1].upper() if "-" in ticker else None
            market.inning_scope = "full_game"
            market.settlement_rule_status = (
                "paper_supported" if match.validation_status == "confirmed_for_paper" else "needs_review"
            )
            if fetch_orderbooks and match.mapping_status != "rejected_multivariate":
                _fetch_orderbook(client, market)
            elif match.mapping_status == "rejected_multivariate":
                _clear_orderbook_prices(market)
                market.orderbook_raw = {"skipped": "multivariate market"}
            session.add(market)
            session.flush()
            _upsert_mapping(session, game, market, resolution, match)
            summary["markets_upserted"] = int(summary["markets_upserted"]) + 1
            summary["mappings_created_or_updated"] = int(summary["mappings_created_or_updated"]) + 1
            _record_mapping_count(summary, match.mapping_status)

    if settings.kalshi_enable_broad_discovery:
        logger.info("Running bounded Kalshi broad discovery diagnostic.")
        summary["broad_discovery"] = _run_broad_discovery_diagnostic(client, max_pages=max_pages)

    session.commit()
    return summary


def resolve_preview_for_date(session: Session, target_date: date, *, query_kalshi: bool = True) -> dict[str, object]:
    client = KalshiClient.from_market_data_settings()
    games = games_for_eastern_date(session, target_date)
    previews = [resolve_game_markets(client, game, query_kalshi=query_kalshi).to_preview_dict() for game in games]
    partial_errors = [
        {"game_id": preview.get("game_id"), **error}
        for preview in previews
        for error in preview.get("errors", [])
        if isinstance(error, dict)
    ]
    warnings = [
        {
            "game_id": preview.get("game_id"),
            "game_label": preview.get("game_label"),
            "message": "NO_MATCHING_KALSHI_MARKET",
        }
        for preview in previews
        if preview.get("validation_status") == "no_match"
    ]
    return {
        "date": target_date.isoformat(),
        "games_considered": len(games),
        "games": previews,
        "partial_errors": partial_errors,
        "warnings": warnings,
        "errors": partial_errors,
    }
