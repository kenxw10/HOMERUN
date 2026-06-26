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

RESOLVER_MODE = "deterministic_ticker_registry_v1"
SUPPORTED_TARGETED_CURRENT = "supported_targeted_current"
DETERMINISTIC_OBSERVED_PENDING_VALIDATION = "deterministic_observed_pending_validation"
DISCOVERED_UNVERIFIED = "discovered_unverified"
UNKNOWN_PENDING_DISCOVERY = "unknown_pending_discovery"
RETIRED_LEGACY_PREFIXES_NOT_USED = [
    "KXMLBF5GAME",
    "KXMLB5GAME",
    "KXMLBFFGAME",
    "KXMLB5SPREAD",
    "KXMLBFFSPREAD",
    "KXMLB5TOTAL",
    "KXMLBFFTOTAL",
    "KXMLBGAMESPREAD",
    "KXMLBRUNLINE",
    "KXMLBGAMETOTAL",
    "KXMLBRUNSTOTAL",
]

FULL_REGISTRY: dict[str, dict[str, object]] = {
    "full_game_winner": {
        "family_key": "full_game_winner",
        "display_name": "Full Game Winner",
        "status": SUPPORTED_TARGETED_CURRENT,
        "series_ticker": "KXMLBGAME",
        "candidate_series_tickers": ["KXMLBGAME"],
        "notes": "Confirmed targeted resolver and paper trading family.",
    },
    "full_game_spread": {
        "family_key": "full_game_spread",
        "display_name": "Full Game Spread",
        "status": DETERMINISTIC_OBSERVED_PENDING_VALIDATION,
        "series_ticker": "KXMLBSPREAD",
        "candidate_series_tickers": ["KXMLBSPREAD"],
        "notes": "Deterministic observed dashboard-export prefix; discovery-only and not trade-enabled.",
    },
    "full_game_total": {
        "family_key": "full_game_total",
        "display_name": "Full Game Total",
        "status": DETERMINISTIC_OBSERVED_PENDING_VALIDATION,
        "series_ticker": "KXMLBTOTAL",
        "candidate_series_tickers": ["KXMLBTOTAL"],
        "notes": "Deterministic observed dashboard-export prefix; discovery-only and not trade-enabled.",
    },
    "first_five_winner": {
        "family_key": "first_five_winner",
        "display_name": "First Five Winner",
        "status": DETERMINISTIC_OBSERVED_PENDING_VALIDATION,
        "series_ticker": "KXMLBF5",
        "candidate_series_tickers": ["KXMLBF5"],
        "notes": "Deterministic observed dashboard-export prefix; discovery-only and may include tie outcomes.",
    },
    "first_five_spread": {
        "family_key": "first_five_spread",
        "display_name": "First Five Spread",
        "status": DETERMINISTIC_OBSERVED_PENDING_VALIDATION,
        "series_ticker": "KXMLBF5SPREAD",
        "candidate_series_tickers": ["KXMLBF5SPREAD"],
        "notes": "Deterministic observed dashboard-export prefix; discovery-only and not trade-enabled.",
    },
    "first_five_total": {
        "family_key": "first_five_total",
        "display_name": "First Five Total",
        "status": DETERMINISTIC_OBSERVED_PENDING_VALIDATION,
        "series_ticker": "KXMLBF5TOTAL",
        "candidate_series_tickers": ["KXMLBF5TOTAL"],
        "notes": "Deterministic observed dashboard-export prefix; discovery-only and not trade-enabled.",
    },
}

TARGET_FAMILIES = [
    "full_game_winner",
    "full_game_spread",
    "full_game_total",
    "first_five_winner",
    "first_five_spread",
    "first_five_total",
]

DISCOVERY_FAMILIES = [family_key for family_key in TARGET_FAMILIES if family_key != "full_game_winner"]


@dataclass
class DiscoveryProbe:
    family_key: str
    candidate_series_ticker: str
    candidate_event_ticker: str | None
    candidate_market_ticker: str | None
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


def _market_line_text(market: dict[str, Any]) -> str:
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
        )
    ).lower()


def _ticker_text(market: dict[str, Any]) -> str:
    return " ".join(
        str(market.get(key) or "")
        for key in ("ticker", "event_ticker", "series_ticker")
    ).upper()


def _classify_candidate_family(market: dict[str, Any], fallback: str) -> str:
    ticker_text = _ticker_text(market)
    if "KXMLBTEAMTOTAL" in ticker_text:
        return "unsupported_team_total"
    for family_key, definition in FULL_REGISTRY.items():
        prefix = definition.get("series_ticker")
        if isinstance(prefix, str) and re.search(rf"(^|\s){re.escape(prefix)}-", ticker_text):
            return family_key

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


def _parse_explicit_line_text(market: dict[str, Any]) -> Decimal | None:
    text = _market_line_text(market)
    patterns = (
        r"\b(?:spread|run line|runline|handicap|line|total|over/under|over|under)\s*(?:of|is|at|:)?\s*([+-]?\d+(?:\.\d+)?)\b",
        r"(?<![A-Z0-9])([+-]\d+(?:\.\d+)?)(?![A-Z0-9])",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            parsed = _decimal(match.group(1))
            if parsed is not None:
                return parsed
    return None


def _parse_ticker_tail_line(market: dict[str, Any]) -> Decimal | None:
    ticker = str(market.get("ticker") or "").upper()
    if not ticker:
        return None

    selected_team_line = re.search(r"-[A-Z0-9]{2,5}([+-]\d+(?:\.\d+)?)$", ticker)
    if selected_team_line:
        parsed = _decimal(selected_team_line.group(1))
        if parsed is not None:
            return parsed

    tail = ticker.rsplit("-", 1)[-1]
    if re.fullmatch(r"\+?\d+(?:\.\d+)?", tail):
        return _decimal(tail.lstrip("+"))
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

    explicit_line = _parse_explicit_line_text(market)
    if explicit_line is not None:
        return explicit_line

    return _parse_ticker_tail_line(market)


def _selection_code(market: dict[str, Any]) -> str | None:
    ticker = str(market.get("ticker") or "").upper()
    if "-" not in ticker:
        return None
    ticker_parts = ticker.split("-")
    selected_part = ticker_parts[-1]
    if len(ticker_parts) >= 3 and _decimal(selected_part) is not None:
        selected_part = ticker_parts[-2]
    else:
        compact_line = re.match(r"^([A-Z0-9]{2,5})[+-]\d+(?:\.\d+)?$", selected_part)
        if compact_line:
            selected_part = compact_line.group(1)
    selected = re.sub(r"[^A-Z0-9]", "", selected_part)
    return selected or None


def _over_under_side(market: dict[str, Any]) -> str | None:
    ticker = str(market.get("ticker") or "").upper()
    tail = ticker.rsplit("-", 1)[-1] if ticker else ""
    if tail in {"O", "OVER"}:
        return "over"
    if tail in {"U", "UNDER"}:
        return "under"

    text = _market_text(market)
    if re.search(r"\bover\b", text):
        return "over"
    if re.search(r"\bunder\b", text):
        return "under"
    return None


def _classification_metadata(
    market: dict[str, Any],
    classified_family: str,
    line_value: Decimal | None,
) -> dict[str, object]:
    ticker_text = _ticker_text(market)
    text = _market_text(market)
    has_rules = bool(market.get("rules_primary") or market.get("rules_secondary") or market.get("rules"))
    return {
        "family_key": classified_family,
        "selected_team_code": _selection_code(market),
        "line_value": str(line_value) if line_value is not None else None,
        "over_under_side": _over_under_side(market),
        "inning_scope": "first_five" if "F5" in ticker_text or classified_family.startswith("first_five") else "full_game",
        "has_multiple_child_outcomes": bool(re.search(r"\btie\b", text)) or "-TIE" in ticker_text,
        "settlement_rule_status": "text_present_unverified" if has_rules else "unknown_pending_validation",
        "classifier_inputs": [
            "ticker",
            "event_ticker",
            "title",
            "subtitle",
            "yes_subtitle",
            "no_subtitle",
            "rules",
            "custom_strike",
            "functional_strike",
        ],
    }


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


def _matches_game_candidate_event(game: MlbGame, family_key: str, market: dict[str, Any]) -> bool:
    event_tickers = {event_ticker for _series_ticker, event_ticker in _event_ticker_candidates(game, family_key)}
    returned_event_ticker = str(market.get("event_ticker") or "").upper()
    returned_ticker = str(market.get("ticker") or "").upper()
    if returned_event_ticker in event_tickers:
        return True
    return any(returned_ticker.startswith(f"{event_ticker}-") for event_ticker in event_tickers)


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
    raw_payload["pr3a_classification"] = _classification_metadata(market, classified_family, line_value)

    return MarketFamilyDiscoveryItem(
        run_id=run_id,
        mlb_game_id=game.id,
        family_key=family_key,
        candidate_series_ticker=probe.candidate_series_ticker,
        candidate_event_ticker=probe.candidate_event_ticker,
        candidate_market_ticker=probe.candidate_market_ticker,
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


def _is_not_found_error(exc: Exception) -> bool:
    return isinstance(exc, KalshiAPIError) and exc.source.status_code == 404


def _probe_attempt_record(
    *,
    game: MlbGame,
    probe: DiscoveryProbe,
    outcome: str,
    markets_found: int,
    error: dict[str, object] | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "outcome": outcome,
        "game_id": game.id,
        "external_game_id": game.external_game_id,
        "family_key": probe.family_key,
        "candidate_series_ticker": probe.candidate_series_ticker,
        "candidate_event_ticker": probe.candidate_event_ticker,
        "candidate_market_ticker": probe.candidate_market_ticker,
        "source_strategy": probe.source_strategy,
        "markets_found": markets_found,
    }
    if error is not None:
        record["error"] = error
    return record


def _record_probe_exception(
    *,
    game: MlbGame,
    probe: DiscoveryProbe,
    exc: Exception,
    warnings: list[object],
    errors: list[object],
    probe_attempts: list[dict[str, object]],
    markets_found: int = 0,
) -> None:
    detail = _kalshi_error(exc)
    if _is_not_found_error(exc):
        record = _probe_attempt_record(
            game=game,
            probe=probe,
            outcome="partial_no_match" if markets_found else "no_match",
            markets_found=markets_found,
            error=detail,
        )
        record["message"] = "MARKET_FAMILY_PROBE_NO_MATCH"
        warnings.append(record)
        probe_attempts.append(record)
        return

    record = _probe_attempt_record(
        game=game,
        probe=probe,
        outcome="partial_error" if markets_found else "error",
        markets_found=markets_found,
        error=detail,
    )
    record["message"] = "MARKET_FAMILY_PROBE_ERROR"
    errors.append(record)
    probe_attempts.append(record)


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
    family_order = list(dict.fromkeys(families))
    by_family = {family_key: _empty_family_summary(family_key) for family_key in family_order}
    coverage: dict[str, set[int]] = {family_key: set() for family_key in by_family}
    parsed_lines: dict[str, int] = {family_key: 0 for family_key in by_family}

    for item in items:
        family = by_family[item.family_key]
        if item.family_key != "full_game_winner":
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


def _probe_audit_summary(probe_attempts: list[dict[str, object]]) -> dict[str, object]:
    event_tickers = {
        str(attempt["candidate_event_ticker"])
        for attempt in probe_attempts
        if attempt.get("candidate_event_ticker")
    }
    market_tickers = {
        str(attempt["candidate_market_ticker"])
        for attempt in probe_attempts
        if attempt.get("candidate_market_ticker")
    }
    examples: dict[str, dict[str, list[str]]] = {}
    no_match_counts: dict[str, int] = {}

    for attempt in probe_attempts:
        family_key = str(attempt.get("family_key") or "unknown")
        family_examples = examples.setdefault(
            family_key,
            {"event_tickers": [], "market_tickers": []},
        )
        _append_example(family_examples["event_tickers"], attempt.get("candidate_event_ticker"))  # type: ignore[arg-type]
        _append_example(family_examples["market_tickers"], attempt.get("candidate_market_ticker"))  # type: ignore[arg-type]
        if attempt.get("outcome") in {"no_match", "partial_no_match"}:
            no_match_counts[family_key] = no_match_counts.get(family_key, 0) + 1

    return {
        "attempted_event_tickers_count": len(event_tickers),
        "attempted_market_tickers_count": len(market_tickers),
        "attempted_ticker_examples": examples,
        "no_match_counts": no_match_counts,
    }


def _item_examples_by_family(items: list[MarketFamilyDiscoveryItem]) -> dict[str, list[dict[str, object]]]:
    examples: dict[str, list[dict[str, object]]] = {family_key: [] for family_key in TARGET_FAMILIES}
    for item in items:
        family_examples = examples.setdefault(item.family_key, [])
        if len(family_examples) >= 5:
            continue
        family_examples.append(
            {
                "candidate_event_ticker": item.candidate_event_ticker,
                "candidate_market_ticker": item.candidate_market_ticker,
                "returned_ticker": item.returned_ticker,
                "returned_event_ticker": item.returned_event_ticker,
                "selection_code": item.selection_code,
                "line_value": float(item.line_value) if item.line_value is not None else None,
                "source_strategy": item.source_strategy,
            }
        )
    return examples


def _probe_markets(
    client: KalshiClient,
    game: MlbGame,
    family_key: str,
    warnings: list[object],
    errors: list[object],
    probe_attempts: list[dict[str, object]],
) -> list[tuple[DiscoveryProbe, dict[str, Any]]]:
    found: list[tuple[DiscoveryProbe, dict[str, Any]]] = []

    for series_ticker, event_ticker in _event_ticker_candidates(game, family_key):
        direct_probe = DiscoveryProbe(
            family_key,
            series_ticker,
            event_ticker,
            event_ticker,
            "direct_market_lookup",
        )
        try:
            payload = client.get_market(event_ticker)
            markets = _markets_from_payload(payload)
            probe_attempts.append(
                _probe_attempt_record(
                    game=game,
                    probe=direct_probe,
                    outcome="found" if markets else "no_match",
                    markets_found=len(markets),
                )
            )
            found.extend((direct_probe, market) for market in markets)
        except Exception as exc:
            _record_probe_exception(
                game=game,
                probe=direct_probe,
                exc=exc,
                warnings=warnings,
                errors=errors,
                probe_attempts=probe_attempts,
            )

        filter_probe = DiscoveryProbe(
            family_key,
            series_ticker,
            event_ticker,
            None,
            "event_ticker_filter",
        )
        try:
            payload = client.get_markets_by_event_ticker(event_ticker)
            markets = _markets_from_payload(payload)
            probe_attempts.append(
                _probe_attempt_record(
                    game=game,
                    probe=filter_probe,
                    outcome="found" if markets else "no_match",
                    markets_found=len(markets),
                )
            )
            found.extend((filter_probe, market) for market in markets)
        except Exception as exc:
            _record_probe_exception(
                game=game,
                probe=filter_probe,
                exc=exc,
                warnings=warnings,
                errors=errors,
                probe_attempts=probe_attempts,
            )

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
    families = TARGET_FAMILIES
    games = _games_for_date(session, day)
    errors: list[object] = []
    warnings: list[object] = []
    probe_attempts: list[dict[str, object]] = []
    persisted_items: list[MarketFamilyDiscoveryItem] = []
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
    run_id = run.id
    session.commit()
    session.refresh(run)

    if not settings.market_family_discovery_enabled:
        completed = utc_now()
        result = {
            "run_id": run.id,
            "date": day.isoformat(),
            "resolver_mode": RESOLVER_MODE,
            "status": "skipped",
            "games_considered": len(games),
            "families_considered": len(families),
            "attempted_event_tickers_count": 0,
            "attempted_market_tickers_count": 0,
            "markets_found": 0,
            "by_family": _summarize_by_family(families, [], completed),
            "warnings": [{"message": "MARKET_FAMILY_DISCOVERY_DISABLED"}],
            "errors": [],
            "attempted_probe_count": 0,
            "probe_attempts": [],
            "attempted_ticker_examples": {},
            "no_match_counts": {},
            "retired_legacy_prefixes_not_used": RETIRED_LEGACY_PREFIXES_NOT_USED,
        }
        run.status = "skipped"
        run.completed_at = completed
        run.markets_found = 0
        run.warnings = result["warnings"]  # type: ignore[assignment]
        run.errors = []
        run.raw_summary = result
        session.add(run)
        session.commit()
        return result

    try:
        kalshi_client = client or KalshiClient.from_market_data_settings()
        seen: set[tuple[object, ...]] = set()

        for game in games:
            for family_key in families:
                for probe, market in _probe_markets(
                    kalshi_client,
                    game,
                    family_key,
                    warnings,
                    errors,
                    probe_attempts,
                ):
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
                    dedupe_key = (family_key, ticker)
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
        audit = _probe_audit_summary(probe_attempts)
        status_value = "partial_error" if errors else "completed"
        result = {
            "run_id": run.id,
            "date": day.isoformat(),
            "resolver_mode": RESOLVER_MODE,
            "status": status_value,
            "games_considered": len(games),
            "families_considered": len(families),
            **audit,
            "markets_found": len(persisted_items),
            "by_family": by_family,
            "warnings": warnings,
            "errors": errors,
            "attempted_probe_count": len(probe_attempts),
            "probe_attempts": probe_attempts,
            "retired_legacy_prefixes_not_used": RETIRED_LEGACY_PREFIXES_NOT_USED,
        }
        run.completed_at = completed
        run.status = status_value
        run.markets_found = len(persisted_items)
        run.errors = errors
        run.warnings = warnings
        run.raw_summary = result
        session.add(run)
        session.commit()
        return result
    except Exception as exc:
        session.rollback()
        completed = utc_now()
        error = {"message": str(exc), "type": exc.__class__.__name__}
        audit = _probe_audit_summary(probe_attempts)
        result = {
            "run_id": run_id,
            "date": day.isoformat(),
            "resolver_mode": RESOLVER_MODE,
            "status": "failed",
            "games_considered": len(games),
            "families_considered": len(families),
            **audit,
            "markets_found": 0,
            "by_family": _summarize_by_family(families, [], completed),
            "warnings": warnings,
            "errors": [*errors, error],
            "attempted_probe_count": len(probe_attempts),
            "probe_attempts": probe_attempts,
            "retired_legacy_prefixes_not_used": RETIRED_LEGACY_PREFIXES_NOT_USED,
        }
        failed_run = session.get(MarketFamilyDiscoveryRun, run_id)
        if failed_run is None:
            raise
        failed_run.completed_at = completed
        failed_run.status = "failed"
        failed_run.markets_found = 0
        failed_run.errors = result["errors"]  # type: ignore[assignment]
        failed_run.warnings = warnings
        failed_run.raw_summary = result
        session.add(failed_run)
        session.commit()
        return result


def latest_market_family_discovery(session: Session, target_date: date | None = None) -> dict[str, object]:
    day = target_date or today_eastern()
    candidate_runs = list(
        session.scalars(
            select(MarketFamilyDiscoveryRun)
            .where(MarketFamilyDiscoveryRun.target_date == day)
            .where(MarketFamilyDiscoveryRun.status != "running")
            .order_by(MarketFamilyDiscoveryRun.started_at.desc(), MarketFamilyDiscoveryRun.id.desc())
            .limit(20)
        )
    )
    run = next(
        (
            candidate_run
            for candidate_run in candidate_runs
            if (candidate_run.raw_summary or {}).get("resolver_mode") == RESOLVER_MODE
        ),
        None,
    )
    if run is None:
        return {
            "date": day.isoformat(),
            "resolver_mode": RESOLVER_MODE,
            "run": None,
            "by_family": {family_key: _empty_family_summary(family_key) for family_key in TARGET_FAMILIES},
            "attempted_event_tickers_count": 0,
            "attempted_market_tickers_count": 0,
            "attempted_ticker_examples": {},
            "no_match_counts": {},
            "item_examples_by_family": {family_key: [] for family_key in TARGET_FAMILIES},
            "retired_legacy_prefixes_not_used": RETIRED_LEGACY_PREFIXES_NOT_USED,
            "items": [],
        }

    items = list(
        session.scalars(
            select(MarketFamilyDiscoveryItem)
            .where(MarketFamilyDiscoveryItem.run_id == run.id)
            .order_by(MarketFamilyDiscoveryItem.family_key.asc(), MarketFamilyDiscoveryItem.returned_ticker.asc())
        )
    )
    raw_summary = run.raw_summary or {}
    return {
        "date": day.isoformat(),
        "resolver_mode": raw_summary.get("resolver_mode", RESOLVER_MODE),
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
        "by_family": raw_summary.get("by_family")
        or _summarize_by_family(TARGET_FAMILIES, items, run.completed_at or utc_now()),
        "attempted_event_tickers_count": raw_summary.get("attempted_event_tickers_count", 0),
        "attempted_market_tickers_count": raw_summary.get("attempted_market_tickers_count", 0),
        "attempted_ticker_examples": raw_summary.get("attempted_ticker_examples", {}),
        "no_match_counts": raw_summary.get("no_match_counts", {}),
        "item_examples_by_family": _item_examples_by_family(items),
        "retired_legacy_prefixes_not_used": raw_summary.get(
            "retired_legacy_prefixes_not_used",
            RETIRED_LEGACY_PREFIXES_NOT_USED,
        ),
        "attempted_probe_count": raw_summary.get("attempted_probe_count", 0),
        "probe_attempts": raw_summary.get("probe_attempts", []),
        "items": [
            {
                "family_key": item.family_key,
                "candidate_series_ticker": item.candidate_series_ticker,
                "candidate_event_ticker": item.candidate_event_ticker,
                "candidate_market_ticker": item.candidate_market_ticker,
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
        "mode": "planned_probes_only",
        "resolver_mode": RESOLVER_MODE,
        "games_considered": len(games),
        "families_considered": len(TARGET_FAMILIES),
        "by_family": {family_key: _empty_family_summary(family_key) for family_key in TARGET_FAMILIES},
        "retired_legacy_prefixes_not_used": RETIRED_LEGACY_PREFIXES_NOT_USED,
        "probes": [
            {
                "game_id": game.id,
                "game": f"{game.away_team} @ {game.home_team}",
                "family_key": family_key,
                "candidate_event_tickers": [
                    event_ticker for _series_ticker, event_ticker in _event_ticker_candidates(game, family_key)
                ][:9],
                "candidate_market_tickers": [
                    event_ticker for _series_ticker, event_ticker in _event_ticker_candidates(game, family_key)
                ][:9],
                "candidate_series_tickers": FULL_REGISTRY[family_key]["candidate_series_tickers"],
            }
            for game in games
            for family_key in TARGET_FAMILIES
        ],
    }
