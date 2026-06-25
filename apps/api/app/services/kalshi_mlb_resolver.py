from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
import logging
import re
from typing import Any
from zoneinfo import ZoneInfo

from app.models import MlbGame
from app.services.kalshi import KalshiAPIError, KalshiClient
from app.time_utils import eastern_display, ensure_aware_utc, to_eastern_iso

logger = logging.getLogger(__name__)

TARGETED_SERIES_TICKER = "KXMLBGAME"
EVENT_OFFSETS_MINUTES = (0, -5, 5, -10, 10, -1, 1)
SERIES_FALLBACK_CLOSE_LOOKBACK = timedelta(days=1)
SERIES_FALLBACK_CLOSE_LOOKAHEAD = timedelta(days=21)
MONTH_CODES = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
MONTH_CODE_TO_NUMBER = {code: index + 1 for index, code in enumerate(MONTH_CODES)}
KALSHI_EVENT_TIME_ZONE = ZoneInfo("America/New_York")

MARKET_FAMILY_REGISTRY: dict[str, dict[str, str]] = {
    "full_game_winner": {"series_ticker": TARGETED_SERIES_TICKER, "status": "supported_targeted"},
    "full_game_spread": {"status": "unknown_pending_discovery"},
    "full_game_total": {"status": "unknown_pending_discovery"},
    "first_five_winner": {"status": "unknown_pending_discovery"},
    "first_five_spread": {"status": "unknown_pending_discovery"},
    "first_five_total": {"status": "unknown_pending_discovery"},
}

TEAM_CODE_OVERRIDES = {
    "arizona diamondbacks": "ARI",
    "athletics": "ATH",
    "boston red sox": "BOS",
    "chicago cubs": "CHC",
    "chicago white sox": "CWS",
    "kansas city royals": "KC",
    "los angeles angels": "LAA",
    "los angeles dodgers": "LAD",
    "new york mets": "NYM",
    "new york yankees": "NYY",
    "san diego padres": "SD",
    "san francisco giants": "SF",
    "st louis cardinals": "STL",
    "st. louis cardinals": "STL",
    "tampa bay rays": "TB",
    "toronto blue jays": "TOR",
    "washington nationals": "WSH",
    "houston astros": "HOU",
    "detroit tigers": "DET",
    "cleveland guardians": "CLE",
    "texas rangers": "TEX",
    "seattle mariners": "SEA",
    "philadelphia phillies": "PHI",
    "pittsburgh pirates": "PIT",
    "cincinnati reds": "CIN",
    "milwaukee brewers": "MIL",
    "minnesota twins": "MIN",
    "colorado rockies": "COL",
    "miami marlins": "MIA",
    "baltimore orioles": "BAL",
    "atlanta braves": "ATL",
}


@dataclass
class ResolvedMarket:
    market: dict[str, Any]
    resolver_strategy: str
    mapping_status: str
    validation_status: str
    confidence: Decimal
    rationale: str
    metadata: dict[str, object]


@dataclass
class GameResolution:
    game_id: int | None
    game_label: str
    scheduled_start: str | None
    scheduled_start_display: str | None
    home_abbreviation: str
    away_abbreviation: str
    attempted_event_tickers: list[str]
    attempted_market_tickers: list[str]
    likely_resolver_strategy: str = "exact_market_tickers"
    matches: list[ResolvedMarket] = field(default_factory=list)
    errors: list[dict[str, object]] = field(default_factory=list)

    def to_preview_dict(self) -> dict[str, object]:
        return {
            "game_id": self.game_id,
            "game_label": self.game_label,
            "scheduled_start": self.scheduled_start,
            "scheduled_start_display": self.scheduled_start_display,
            "home_abbreviation": self.home_abbreviation,
            "away_abbreviation": self.away_abbreviation,
            "attempted_event_tickers": self.attempted_event_tickers,
            "attempted_market_tickers": self.attempted_market_tickers,
            "likely_resolver_strategy": self.likely_resolver_strategy,
            "matching_markets": [
                {
                    "ticker": str(match.market.get("ticker") or ""),
                    "event_ticker": str(match.market.get("event_ticker") or ""),
                    "status": str(match.market.get("status") or ""),
                    "title": str(match.market.get("title") or ""),
                    "resolver_strategy": match.resolver_strategy,
                    "mapping_status": match.mapping_status,
                    "validation_status": match.validation_status,
                    "confidence": float(match.confidence),
                    "rationale": match.rationale,
                    "metadata": match.metadata,
                }
                for match in self.matches
            ],
            "validation_status": "matched" if self.matches else "no_match",
            "errors": self.errors,
        }


def _has_usable_match(resolution: GameResolution) -> bool:
    return any(not match.mapping_status.startswith("rejected_") for match in resolution.matches)


def _normalize_name(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


def _clean_code(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"[^A-Za-z0-9]", "", value).upper()
    return cleaned or None


def normalize_team_abbreviation(team_name: str, raw_abbreviation: str | None = None) -> str:
    normalized_name = _normalize_name(team_name)
    if normalized_name in TEAM_CODE_OVERRIDES:
        return TEAM_CODE_OVERRIDES[normalized_name]

    raw = _clean_code(raw_abbreviation)
    if raw:
        return raw

    words = normalized_name.split()
    if not words:
        return "UNK"
    if len(words) == 1:
        return words[0][:3].upper()
    return "".join(word[0] for word in words)[-3:].upper()


def game_team_codes(game: MlbGame) -> tuple[str, str]:
    away = normalize_team_abbreviation(game.away_team, getattr(game, "away_abbreviation", None))
    home = normalize_team_abbreviation(game.home_team, getattr(game, "home_abbreviation", None))
    return away, home


def _event_timestamp(value: datetime) -> str:
    local = ensure_aware_utc(value).astimezone(KALSHI_EVENT_TIME_ZONE)
    return f"{local:%y}{MONTH_CODES[local.month - 1]}{local:%d%H%M}"


def build_event_ticker_candidates(game: MlbGame) -> list[str]:
    away_code, home_code = game_team_codes(game)
    candidates: list[str] = []
    for offset in EVENT_OFFSETS_MINUTES:
        timestamp = _event_timestamp(ensure_aware_utc(game.scheduled_start) + timedelta(minutes=offset))
        ticker = f"{TARGETED_SERIES_TICKER}-{timestamp}{away_code}{home_code}"
        if ticker not in candidates:
            candidates.append(ticker)
    return candidates


def build_market_ticker_candidates(game: MlbGame) -> list[str]:
    away_code, home_code = game_team_codes(game)
    tickers: list[str] = []
    for event_ticker in build_event_ticker_candidates(game):
        for suffix in (away_code, home_code):
            ticker = f"{event_ticker}-{suffix}"
            if ticker not in tickers:
                tickers.append(ticker)
    return tickers


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
            "yes_subtitle",
            "no_sub_title",
            "no_subtitle",
            "ticker",
            "event_ticker",
        )
    ).lower()


def is_multivariate_market(market: dict[str, Any]) -> bool:
    event_ticker = str(market.get("event_ticker") or "").upper()
    ticker = str(market.get("ticker") or "").upper()
    return bool(market.get("mve_selected_legs")) or event_ticker.startswith("KXMVE") or ticker.startswith("KXMV")


def _market_relevant_time(market: dict[str, Any]) -> datetime | None:
    from app.time_utils import parse_datetime

    return parse_datetime(market.get("occurrence_datetime") or market.get("expected_expiration_time") or market.get("close_time"))


def _event_ticker_from_market(market: dict[str, Any]) -> str:
    event_ticker = str(market.get("event_ticker") or "").upper()
    if event_ticker:
        return event_ticker
    ticker = str(market.get("ticker") or "").upper()
    return ticker.rsplit("-", 1)[0] if ticker.count("-") >= 2 else ""


def _event_ticker_details_for_game(game: MlbGame, event_ticker: str) -> dict[str, object] | None:
    match = re.match(
        r"^KXMLBGAME-(?P<year>\d{2})(?P<month>[A-Z]{3})(?P<day>\d{2})"
        r"(?P<hour>\d{2})(?P<minute>\d{2})(?P<teams>[A-Z0-9]+)$",
        event_ticker.upper(),
    )
    if not match:
        return None
    month = MONTH_CODE_TO_NUMBER.get(match.group("month"))
    if month is None:
        return None

    local = datetime(
        2000 + int(match.group("year")),
        month,
        int(match.group("day")),
        int(match.group("hour")),
        int(match.group("minute")),
        tzinfo=KALSHI_EVENT_TIME_ZONE,
    )
    away_code, home_code = game_team_codes(game)
    ticker_codes = str(match.group("teams"))
    return {
        "timestamp": ensure_aware_utc(local),
        "ticker_team_codes": ticker_codes,
        "expected_team_codes": f"{away_code}{home_code}",
        "team_codes_match": ticker_codes == f"{away_code}{home_code}",
    }


def _time_delta_minutes(game: MlbGame, market: dict[str, Any], attempted_event_tickers: list[str]) -> int | None:
    event_details = _event_ticker_details_for_game(game, _event_ticker_from_market(market))
    if event_details is not None:
        return int(
            abs(
                (event_details["timestamp"] - ensure_aware_utc(game.scheduled_start)).total_seconds()
            )
            / 60
        )

    market_time = _market_relevant_time(market)
    if market_time is None:
        event_ticker = str(market.get("event_ticker") or "").upper()
        ticker = str(market.get("ticker") or "").upper()
        if event_ticker in attempted_event_tickers or any(ticker.startswith(f"{event}-") for event in attempted_event_tickers):
            return 0
        return None
    return int(abs((market_time - ensure_aware_utc(game.scheduled_start)).total_seconds()) / 60)


def _team_match_score(game: MlbGame, market: dict[str, Any]) -> Decimal:
    text = _market_text(market)
    ticker = str(market.get("ticker") or "").upper()
    away_code, home_code = game_team_codes(game)
    event_details = _event_ticker_details_for_game(game, _event_ticker_from_market(market))
    selected = ticker.rsplit("-", 1)[-1] if "-" in ticker else ""
    if event_details and event_details.get("team_codes_match"):
        score = Decimal("0.75")
        if selected in {away_code, home_code}:
            score += Decimal("0.25")
        return min(score, Decimal("1.00"))

    score = Decimal("0")
    for team_name, code in ((game.away_team, away_code), (game.home_team, home_code)):
        normalized_team = _normalize_name(team_name)
        compact_team = normalized_team.replace(" ", "")
        if normalized_team and normalized_team in text:
            score += Decimal("0.25")
        elif compact_team and compact_team in text.replace(" ", ""):
            score += Decimal("0.25")
        elif code and (ticker.endswith(f"-{code}") or f"-{code}" in ticker):
            score += Decimal("0.25")
    if ticker.endswith(f"-{away_code}") or ticker.endswith(f"-{home_code}"):
        score += Decimal("0.25")
    return min(score, Decimal("1.00"))


def validate_market_for_game(
    game: MlbGame,
    market: dict[str, Any],
    attempted_event_tickers: list[str],
    attempted_market_tickers: list[str],
    strategy: str,
) -> ResolvedMarket:
    ticker = str(market.get("ticker") or "").upper()
    event_ticker = str(market.get("event_ticker") or "").upper()
    event_details = _event_ticker_details_for_game(game, _event_ticker_from_market(market))
    notes: list[str] = []
    time_delta = _time_delta_minutes(game, market, attempted_event_tickers)
    team_score = _team_match_score(game, market)
    market_ticker_match = ticker in attempted_market_tickers
    event_ticker_match = event_ticker in attempted_event_tickers or any(
        ticker.startswith(f"{candidate}-") for candidate in attempted_event_tickers
    )

    if is_multivariate_market(market):
        notes.append("REJECTED_MULTIVARIATE")
        mapping_status = "rejected_multivariate"
        validation_status = "rejected_multivariate"
        confidence = Decimal("0.0000")
    elif market_ticker_match and event_ticker_match and (time_delta is None or time_delta <= 10) and team_score >= Decimal("0.50"):
        notes.extend(["MARKET_TICKER_MATCH", "EVENT_TICKER_MATCH", "TICKER_TEAM_CODE_MATCH"])
        mapping_status = "confirmed"
        validation_status = "confirmed_for_paper"
        confidence = Decimal("0.9700")
    elif event_ticker_match and (time_delta is None or time_delta <= 360) and team_score >= Decimal("0.25"):
        notes.append("EVENT_TICKER_MATCH")
        mapping_status = "candidate"
        validation_status = "strong_candidate"
        confidence = Decimal("0.8500")
    elif strategy == "series_window_fallback":
        notes.append("REJECTED_UNRELATED_FALLBACK_MARKET")
        mapping_status = "rejected_unrelated"
        validation_status = "rejected_unrelated"
        confidence = Decimal("0.0000")
    else:
        notes.append("TARGETED_MATCH_UNCERTAIN")
        mapping_status = "needs_review"
        validation_status = "needs_review"
        confidence = Decimal("0.4500")

    if time_delta is not None:
        notes.append(f"TIME_DELTA_MINUTES:{time_delta}")
    notes.append(f"TEAM_MATCH_SCORE:{team_score}")

    metadata = {
        "market_family": "full_game_winner",
        "market_family_registry": MARKET_FAMILY_REGISTRY,
        "resolver_strategy_used": strategy,
        "returned_event_tickers": [event_ticker] if event_ticker else [],
        "returned_market_tickers": [ticker] if ticker else [],
        "time_delta_minutes": time_delta,
        "team_match_score": float(team_score),
        "ticker_event_time": (
            event_details["timestamp"].isoformat()
            if event_details and isinstance(event_details.get("timestamp"), datetime)
            else None
        ),
        "ticker_team_codes": event_details.get("ticker_team_codes") if event_details else None,
        "expected_team_codes": event_details.get("expected_team_codes") if event_details else None,
        "ticker_team_codes_match": bool(event_details and event_details.get("team_codes_match")),
        "validation_notes": notes,
        "mve_filter": "exclude",
    }
    return ResolvedMarket(
        market=market,
        resolver_strategy=strategy,
        mapping_status=mapping_status,
        validation_status=validation_status,
        confidence=confidence,
        rationale="; ".join(notes),
        metadata=metadata,
    )


def _error_detail(exc: KalshiAPIError, *, fallback_attempted: bool) -> dict[str, object]:
    detail = exc.to_detail()
    detail["retry_or_fallback_attempted"] = fallback_attempted
    return detail


def _base_resolution(game: MlbGame) -> GameResolution:
    away_code, home_code = game_team_codes(game)
    return GameResolution(
        game_id=game.id,
        game_label=f"{game.away_team} @ {game.home_team}",
        scheduled_start=to_eastern_iso(game.scheduled_start),
        scheduled_start_display=eastern_display(game.scheduled_start),
        home_abbreviation=home_code,
        away_abbreviation=away_code,
        attempted_event_tickers=build_event_ticker_candidates(game),
        attempted_market_tickers=build_market_ticker_candidates(game),
    )


def resolve_game_markets(client: KalshiClient, game: MlbGame, *, query_kalshi: bool = True) -> GameResolution:
    resolution = _base_resolution(game)
    if not query_kalshi:
        return resolution

    seen_tickers: set[str] = set()
    logger.info("Resolving Kalshi MLB markets for game %s", game.external_game_id)

    try:
        payload = client.get_markets_by_tickers(resolution.attempted_market_tickers)
        _add_validated_markets(resolution, game, _markets_from_payload(payload), "exact_market_tickers", seen_tickers)
        if _has_usable_match(resolution):
            return resolution
    except KalshiAPIError as exc:
        logger.warning("Kalshi exact ticker resolver failed: %s", exc)
        resolution.errors.append(_error_detail(exc, fallback_attempted=True))

    for event_ticker in resolution.attempted_event_tickers:
        try:
            payload = client.get_event(event_ticker)
            _add_validated_markets(resolution, game, _markets_from_payload(payload), "get_event", seen_tickers)
            if _has_usable_match(resolution):
                return resolution
        except KalshiAPIError as exc:
            resolution.errors.append(_error_detail(exc, fallback_attempted=True))

    for event_ticker in resolution.attempted_event_tickers:
        try:
            payload = client.get_markets_by_event_ticker(event_ticker)
            _add_validated_markets(resolution, game, _markets_from_payload(payload), "event_ticker_filter", seen_tickers)
            if _has_usable_match(resolution):
                return resolution
        except KalshiAPIError as exc:
            resolution.errors.append(_error_detail(exc, fallback_attempted=True))

    try:
        start = int((ensure_aware_utc(game.scheduled_start) - SERIES_FALLBACK_CLOSE_LOOKBACK).timestamp())
        end = int((ensure_aware_utc(game.scheduled_start) + SERIES_FALLBACK_CLOSE_LOOKAHEAD).timestamp())
        markets = client.get_markets_by_series_window(
            TARGETED_SERIES_TICKER,
            start,
            end,
            limit=100,
            max_pages=2,
        )
        _add_validated_markets(resolution, game, markets, "series_window_fallback", seen_tickers)
    except KalshiAPIError as exc:
        logger.warning("Kalshi series fallback resolver failed: %s", exc)
        resolution.errors.append(_error_detail(exc, fallback_attempted=False))

    return resolution


def _add_validated_markets(
    resolution: GameResolution,
    game: MlbGame,
    markets: list[dict[str, Any]],
    strategy: str,
    seen_tickers: set[str],
) -> None:
    for market in markets:
        ticker = str(market.get("ticker") or "").upper()
        if not ticker or ticker in seen_tickers:
            continue
        seen_tickers.add(ticker)
        match = validate_market_for_game(
            game,
            market,
            resolution.attempted_event_tickers,
            resolution.attempted_market_tickers,
            strategy,
        )
        if match.mapping_status == "rejected_unrelated":
            continue
        resolution.matches.append(match)
