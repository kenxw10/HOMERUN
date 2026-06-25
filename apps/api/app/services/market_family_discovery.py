from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import MarketFamilyDiscoveryItem, MarketFamilyDiscoveryRun, MlbGame
from app.services.kalshi import KalshiAPIError, KalshiClient
from app.services.kalshi_mlb_resolver import build_event_ticker_candidates, is_multivariate_market
from app.time_utils import ensure_aware_utc, get_dashboard_zone, today_eastern, utc_now

SUPPORTED_TARGETED = "supported_targeted"
DISCOVERED_UNVERIFIED = "discovered_unverified"
UNKNOWN_PENDING_DISCOVERY = "unknown_pending_discovery"

FULL_REGISTRY: dict[str, dict[str, object]] = {
    "full_game_winner": {
        "family_key": "full_game_winner",
        "display_name": "Full Game Winner",
        "status": SUPPORTED_TARGETED,
        "series_ticker": "KXMLBGAME",
        "candidate_series_tickers": ["KXMLBGAME"],
        "notes": "Confirmed targeted resolver and paper trading family.",
    },
    "full_game_spread": {
        "family_key": "full_game_spread",
        "display_name": "Full Game Spread",
        "status": UNKNOWN_PENDING_DISCOVERY,
        "series_ticker": None,
        "candidate_series_tickers": ["KXMLBSPREAD", "KXMLBGAMESPREAD", "KXMLBRUNLINE"],
        "notes": "Discovery-only in PR3a; not trade-enabled.",
    },
    "full_game_total": {
        "family_key": "full_game_total",
        "display_name": "Full Game Total",
        "status": UNKNOWN_PENDING_DISCOVERY,
        "series_ticker": None,
        "candidate_series_tickers": ["KXMLBTOTAL", "KXMLBGAMETOTAL", "KXMLBRUNSTOTAL"],
        "notes": "Discovery-only in PR3a; not trade-enabled.",
    },
    "first_five_winner": {
        "family_key": "first_five_winner",
        "display_name": "First Five Winner",
        "status": UNKNOWN_PENDING_DISCOVERY,
        "series_ticker": None,
        "candidate_series_tickers": ["KXMLBF5GAME", "KXMLB5GAME", "KXMLBFFGAME"],
        "notes": "Discovery-only in PR3a; not trade-enabled.",
    },
    "first_five_spread": {
        "family_key": "first_five_spread",
        "display_name": "First Five Spread",
        "status": UNKNOWN_PENDING_DISCOVERY,
        "series_ticker": None,
        "candidate_series_tickers": ["KXMLBF5SPREAD", "KXMLB5SPREAD", "KXMLBFFSPREAD"],
        "notes": "Discovery-only in PR3a; not trade-enabled.",
    },
    "first_five_total": {
        "family_key": "first_five_total",
        "display_name": "First Five Total",
        "status": UNKNOWN_PENDING_DISCOVERY,
        "series_ticker": None,
        "candidate_series_tickers": ["KXMLBF5TOTAL", "KXMLB5TOTAL", "KXMLBFFTOTAL"],
        "notes": "Discovery-only in PR3a; not trade-enabled.",
    },
}

DISCOVERY_FAMILIES = [
    "full_game_spread",
    "full_game_total",
    "first_five_winner",
    "first_five_spread",
    "first_five_total",
]


@dataclass
class DiscoveryProbe:
    family_key: str
    candidate_series_ticker: str
    candidate_event_ticker: str | None
    source_strategy: str


def _date_bounds(target_date: date) -> tuple[datetime, datetime]:
    local_start = datetime.combine(target_date, time.min, tzinfo=get_dashboard_zone())
    start = ensure_aware_utc(local_start)
    return start, start + timedelta(days=1)


def _games_for_date(session: Session, target_date: date) -> list[MlbGame]:
    start, end = _date_bounds(target_date)
    return list(
        session.scalars(
            select(MlbGame)
            .where(MlbGame.scheduled_start >= start)
            .where(MlbGame.scheduled_start < end)
            .order_by(MlbGame.scheduled_start.asc())
        )
    )


def _markets_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    markets = payload.get("markets")
    if isinstance(markets, list):
        return [market for market in markets if isinstance(market, dict)]
    event = payload.get("event")
    if isinstance(event, dict) and isinstance(event.get("markets"), list):
        return [market for market in event["markets"] if isinstance(market, dict)]
    market = payload.get("market")
    if isinstance(market, dict):
        return [market]
    return []


def _market_text(market: dict[str, Any]) -> str:
    return " ".join(
        str(market.get(key) or "")
        for key in (
            "title",
            "subtitle",
            "rules_primary",
            "rules_secondary",
            "rules",
            "yes_sub_title",
            "no_sub_title",
            "yes_subtitle",
            "no_subtitle",
            "ticker",
            "event_ticker",
            "series_ticker",
        )
    ).lower()


def _classify_candidate_family(market: dict[str, Any], fallback: str) -> str:
    text = _market_text(market)
    first_five = bool(re.search(r"\b(first five|first 5|f5|5 innings?)\b", text))
    spread = bool(re.search(r"\b(spread|run line|runline|handicap)\b", text))
    total = bool(re.search(r"\b(total|over|under)\b", text))
    winner = bool(re.search(r"\b(winner|win the game|moneyline)\b", text))

    if first_five and spread:
        return "first_five_spread"
    if first_five and total:
        return "first_five_total"
    if first_five and winner:
        return "first_five_winner"
    if spread:
        return "full_game_spread"
    if total:
        return "full_game_total"
    if winner:
        return "full_game_winner"
    return fallback


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.0001"))
    except Exception:
        return None


def _parse_line_value(market: dict[str, Any]) -> Decimal | None:
    for key in ("line", "strike", "functional_strike"):
        parsed = _decimal(market.get(key))
        if parsed is not None:
            return parsed

    custom = market.get("custom_strike")
    if isinstance(custom, dict):
        for key in ("value", "line", "strike"):
            parsed = _decimal(custom.get(key))
            if parsed is not None:
                return parsed

    text = _market_text(market)
    match = re.search(r"(?<![A-Z0-9])([+-]?\d+(?:\.\d+)?)(?![A-Z0-9])", text, flags=re.IGNORECASE)
    return _decimal(match.group(1)) if match else None


def _selection_code(market: dict[str, Any]) -> str | None:
    ticker = str(market.get("ticker") or "").upper()
    if "-" not in ticker:
        return None
    selected = re.sub(r"[^A-Z0-9]", "", ticker.rsplit("-", 1)[-1])
    return selected or None


def _event_suffixes(game: MlbGame) -> list[str]:
    suffixes: list[str] = []
    for event_ticker in build_event_ticker_candidates(game):
        if "-" not in event_ticker:
            continue
        suffix = event_ticker.split("-", 1)[1]
        if suffix not in suffixes:
            suffixes.append(suffix)
    return suffixes


def _event_ticker_candidates(game: MlbGame, family_key: str) -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    for series_ticker in FULL_REGISTRY[family_key]["candidate_series_tickers"]:
        assert isinstance(series_ticker, str)
        for suffix in _event_suffixes(game):
            values.append((series_ticker, f"{series_ticker}-{suffix}"))
    return values


def _item_from_market(
    *,
    run_id: int,
    game: MlbGame,
    family_key: str,
    probe: DiscoveryProbe,
    market: dict[str, Any],
) -> MarketFamilyDiscoveryItem:
    classified_family = _classify_candidate_family(market, family_key)
    confidence = Decimal("0.5500") if classified_family == family_key else Decimal("0.3000")
    line_value = _parse_line_value(market)
    raw_payload = dict(market)
    raw_payload["pr3a_classified_family"] = classified_family

    return MarketFamilyDiscoveryItem(
        run_id=run_id,
        mlb_game_id=game.id,
        family_key=family_key,
        candidate_series_ticker=probe.candidate_series_ticker,
        candidate_event_ticker=probe.candidate_event_ticker,
        candidate_market_ticker=None,
        returned_ticker=market.get("ticker"),
        returned_event_ticker=market.get("event_ticker"),
        title=market.get("title"),
        subtitle=market.get("subtitle"),
        yes_sub_title=market.get("yes_sub_title") or market.get("yes_subtitle"),
        no_sub_title=market.get("no_sub_title") or market.get("no_subtitle"),
        rules_primary=market.get("rules_primary") or market.get("rules"),
        rules_secondary=market.get("rules_secondary"),
        custom_strike=market.get("custom_strike") if isinstance(market.get("custom_strike"), dict) else None,
        functional_strike=str(market.get("functional_strike")) if market.get("functional_strike") is not None else None,
        status=market.get("status"),
        raw_status=market.get("status"),
        validation_status=DISCOVERED_UNVERIFIED,
        confidence=confidence,
        line_value=line_value,
        selection_code=_selection_code(market),
        source_strategy=probe.source_strategy,
        raw_payload=raw_payload,
    )


def _kalshi_error(exc: Exception) -> dict[str, object]:
    if isinstance(exc, KalshiAPIError):
        return exc.to_detail()
    return {"message": str(exc), "type": exc.__class__.__name__}


def _empty_family_summary(family_key: str) -> dict[str, object]:
    definition = FULL_REGISTRY[family_key]
    return {
        "family_key": family_key,
        "display_name": definition["display_name"],
        "status": definition["status"],
        "series_ticker": definition.get("series_ticker"),
        "candidate_series_tickers_tested": list(definition["candidate_series_tickers"]),
        "discovered_event_ticker_examples": [],
        "discovered_market_ticker_examples": [],
        "market_count": 0,
        "game_coverage_count": 0,
        "line_or_strike_parsing_status": "not_applicable" if family_key.endswith("winner") else UNKNOWN_PENDING_DISCOVERY,
        "settlement_rule_status": "supported_current" if family_key == "full_game_winner" else UNKNOWN_PENDING_DISCOVERY,
        "notes": definition["notes"],
        "last_checked_at": None,
    }


def _append_example(values: list[object], value: str | None, limit: int = 5) -> None:
    if value and value not in values and len(values) < limit:
        values.append(value)


def _summarize_by_family(
    families: list[str],
    items: list[MarketFamilyDiscoveryItem],
    completed_at: datetime,
) -> dict[str, dict[str, object]]:
    by_family = {family_key: _empty_family_summary(family_key) for family_key in ["full_game_winner", *families]}
    coverage: dict[str, set[int]] = {family_key: set() for family_key in by_family}
    parsed_lines: dict[str, int] = {family_key: 0 for family_key in by_family}

    for item in items:
        family = by_family[item.family_key]
        family["status"] = DISCOVERED_UNVERIFIED
        family["market_count"] = int(family["market_count"]) + 1
        if item.mlb_game_id is not None:
            coverage[item.family_key].add(item.mlb_game_id)
        if item.line_value is not None:
            parsed_lines[item.family_key] += 1
        _append_example(family["discovered_event_ticker_examples"], item.returned_event_ticker)  # type: ignore[arg-type]
        _append_example(family["discovered_market_ticker_examples"], item.returned_ticker)  # type: ignore[arg-type]
        if item.rules_primary or item.rules_secondary:
            family["settlement_rule_status"] = "text_present_unverified"

    for family_key, family in by_family.items():
        family["game_coverage_count"] = len(coverage[family_key])
        family["last_checked_at"] = completed_at.isoformat()
        if family_key.endswith(("spread", "total")):
            family["line_or_strike_parsing_status"] = (
                "parsed_unverified"
                if parsed_lines[family_key] > 0
                else ("not_found" if int(family["market_count"]) > 0 else UNKNOWN_PENDING_DISCOVERY)
            )

    return by_family


def _probe_markets(
    client: KalshiClient,
    game: MlbGame,
    family_key: str,
    errors: list[object],
) -> list[tuple[DiscoveryProbe, dict[str, Any]]]:
    settings = get_settings()
    found: list[tuple[DiscoveryProbe, dict[str, Any]]] = []

    for series_ticker, event_ticker in _event_ticker_candidates(game, family_key):
        event_probe = DiscoveryProbe(family_key, series_ticker, event_ticker, "get_event")
        try:
            payload = client.get_event(event_ticker)
            found.extend((event_probe, market) for market in _markets_from_payload(payload))
        except Exception as exc:
            errors.append({"family_key": family_key, "event_ticker": event_ticker, "error": _kalshi_error(exc)})

        filter_probe = DiscoveryProbe(family_key, series_ticker, event_ticker, "event_ticker_filter")
        try:
            payload = client.get_markets_by_event_ticker(event_ticker)
            found.extend((filter_probe, market) for market in _markets_from_payload(payload))
        except Exception as exc:
            errors.append({"family_key": family_key, "event_ticker": event_ticker, "error": _kalshi_error(exc)})

    start = int((ensure_aware_utc(game.scheduled_start) - timedelta(days=1)).timestamp())
    end = int((ensure_aware_utc(game.scheduled_start) + timedelta(days=21)).timestamp())
    for series_ticker in FULL_REGISTRY[family_key]["candidate_series_tickers"]:
        assert isinstance(series_ticker, str)
        probe = DiscoveryProbe(family_key, series_ticker, None, "series_ticker_window")
        try:
            markets = client.iter_markets(
                params={
                    "series_ticker": series_ticker,
                    "min_close_ts": start,
                    "max_close_ts": end,
                    "limit": 100,
                    "mve_filter": "exclude",
                },
                max_pages=settings.market_family_discovery_max_pages,
            )
            found.extend((probe, market) for market in markets)
        except Exception as exc:
            errors.append({"family_key": family_key, "series_ticker": series_ticker, "error": _kalshi_error(exc)})

    return found


def run_market_family_discovery(
    session: Session,
    target_date: date | None = None,
    *,
    client: KalshiClient | None = None,
) -> dict[str, object]:
    settings = get_settings()
    day = target_date or today_eastern()
    started = utc_now()
    families = DISCOVERY_FAMILIES
    games = _games_for_date(session, day)
    run = MarketFamilyDiscoveryRun(
        target_date=day,
        started_at=started,
        status="running",
        games_considered=len(games),
        families_considered=len(families),
        markets_found=0,
        errors=[],
        warnings=[],
        raw_summary={},
    )
    session.add(run)
    session.flush()

    errors: list[object] = []
    warnings: list[object] = []
    persisted_items: list[MarketFamilyDiscoveryItem] = []

    if not settings.market_family_discovery_enabled:
        completed = utc_now()
        result = {
            "run_id": run.id,
            "date": day.isoformat(),
            "games_considered": len(games),
            "families_considered": len(families),
            "markets_found": 0,
            "by_family": _summarize_by_family(families, [], completed),
            "warnings": [{"message": "MARKET_FAMILY_DISCOVERY_DISABLED"}],
            "errors": [],
        }
        run.status = "skipped"
        run.completed_at = completed
        run.warnings = result["warnings"]  # type: ignore[assignment]
        run.raw_summary = result
        session.add(run)
        session.commit()
        return result

    kalshi_client = client or KalshiClient.from_settings()
    seen: set[tuple[int | None, str, str]] = set()

    for game in games:
        for family_key in families:
            for probe, market in _probe_markets(kalshi_client, game, family_key, errors):
                ticker = str(market.get("ticker") or "").upper()
                if not ticker:
                    continue
                if is_multivariate_market(market):
                    warnings.append(
                        {
                            "message": "MULTIVARIATE_MARKET_EXCLUDED",
                            "family_key": family_key,
                            "ticker": ticker,
                        }
                    )
                    continue
                dedupe_key = (game.id, family_key, ticker)
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                item = _item_from_market(
                    run_id=run.id,
                    game=game,
                    family_key=family_key,
                    probe=probe,
                    market=market,
                )
                persisted_items.append(item)
                session.add(item)

    completed = utc_now()
    by_family = _summarize_by_family(families, persisted_items, completed)
    result = {
        "run_id": run.id,
        "date": day.isoformat(),
        "games_considered": len(games),
        "families_considered": len(families),
        "markets_found": len(persisted_items),
        "by_family": by_family,
        "warnings": warnings,
        "errors": errors,
    }
    run.completed_at = completed
    run.status = "completed_with_errors" if errors else "completed"
    run.markets_found = len(persisted_items)
    run.errors = errors
    run.warnings = warnings
    run.raw_summary = result
    session.add(run)
    session.commit()
    return result


def latest_market_family_discovery(session: Session, target_date: date | None = None) -> dict[str, object]:
    day = target_date or today_eastern()
    run = session.scalar(
        select(MarketFamilyDiscoveryRun)
        .where(MarketFamilyDiscoveryRun.target_date == day)
        .order_by(MarketFamilyDiscoveryRun.started_at.desc(), MarketFamilyDiscoveryRun.id.desc())
        .limit(1)
    )
    if run is None:
        return {
            "date": day.isoformat(),
            "run": None,
            "by_family": {
                family_key: _empty_family_summary(family_key) for family_key in ["full_game_winner", *DISCOVERY_FAMILIES]
            },
            "items": [],
        }

    items = list(
        session.scalars(
            select(MarketFamilyDiscoveryItem)
            .where(MarketFamilyDiscoveryItem.run_id == run.id)
            .order_by(MarketFamilyDiscoveryItem.family_key.asc(), MarketFamilyDiscoveryItem.returned_ticker.asc())
        )
    )
    return {
        "date": day.isoformat(),
        "run": {
            "run_id": run.id,
            "started_at": run.started_at.isoformat(),
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            "status": run.status,
            "games_considered": run.games_considered,
            "families_considered": run.families_considered,
            "markets_found": run.markets_found,
            "errors": run.errors or [],
            "warnings": run.warnings or [],
        },
        "by_family": (run.raw_summary or {}).get("by_family")
        or _summarize_by_family(DISCOVERY_FAMILIES, items, run.completed_at or utc_now()),
        "items": [
            {
                "family_key": item.family_key,
                "candidate_series_ticker": item.candidate_series_ticker,
                "candidate_event_ticker": item.candidate_event_ticker,
                "returned_ticker": item.returned_ticker,
                "returned_event_ticker": item.returned_event_ticker,
                "title": item.title,
                "subtitle": item.subtitle,
                "validation_status": item.validation_status,
                "confidence": float(item.confidence) if item.confidence is not None else None,
                "line_value": float(item.line_value) if item.line_value is not None else None,
                "selection_code": item.selection_code,
                "source_strategy": item.source_strategy,
            }
            for item in items
        ],
    }


def market_family_discovery_preview(session: Session, target_date: date | None = None) -> dict[str, object]:
    day = target_date or today_eastern()
    games = _games_for_date(session, day)
    return {
        "date": day.isoformat(),
        "games_considered": len(games),
        "families_considered": len(DISCOVERY_FAMILIES),
        "by_family": {
            family_key: _empty_family_summary(family_key) for family_key in ["full_game_winner", *DISCOVERY_FAMILIES]
        },
        "probes": [
            {
                "game_id": game.id,
                "game": f"{game.away_team} @ {game.home_team}",
                "family_key": family_key,
                "candidate_event_tickers": [
                    event_ticker for _series_ticker, event_ticker in _event_ticker_candidates(game, family_key)
                ][:9],
                "candidate_series_tickers": FULL_REGISTRY[family_key]["candidate_series_tickers"],
            }
            for game in games
            for family_key in DISCOVERY_FAMILIES
        ],
    }
