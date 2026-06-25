from __future__ import annotations

from decimal import Decimal
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import KalshiMarket, MarketMapping, MlbGame
from app.time_utils import ensure_aware_utc


MARKET_HINTS = {
    "first_five_spread": ("first five spread", "first 5 spread", "f5 spread"),
    "first_five_total": ("first five total", "first 5 total", "f5 total"),
    "first_five_moneyline": ("first five", "first 5", "f5", "first-five"),
    "full_game_moneyline": ("moneyline", "winner", "win the game"),
    "full_game_spread": ("spread", "run line"),
    "full_game_total": ("total", "over/under", "runs"),
}


TEAM_ALIAS_OVERRIDES = {
    "arizona diamondbacks": ("diamondbacks", "d-backs", "dbacks"),
    "athletics": ("athletics", "a's"),
    "boston red sox": ("red sox",),
    "chicago white sox": ("white sox",),
    "toronto blue jays": ("blue jays",),
}


def _tokens(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", value.lower())


def _contains_phrase(haystack: list[str], phrase: str) -> bool:
    phrase_tokens = _tokens(phrase)
    if not phrase_tokens:
        return False
    phrase_len = len(phrase_tokens)
    return any(haystack[index : index + phrase_len] == phrase_tokens for index in range(len(haystack) - phrase_len + 1))


def _team_aliases(team: str) -> tuple[str, ...]:
    normalized = " ".join(_tokens(team))
    if not normalized:
        return ()

    aliases = [normalized]
    aliases.extend(TEAM_ALIAS_OVERRIDES.get(normalized, ()))

    team_tokens = normalized.split()
    if normalized not in TEAM_ALIAS_OVERRIDES and len(team_tokens) >= 2:
        aliases.append(team_tokens[-1])

    return tuple(dict.fromkeys(alias for alias in aliases if alias))


def _matches_team(team: str, market_tokens: list[str]) -> bool:
    return any(_contains_phrase(market_tokens, alias) for alias in _team_aliases(team))


def infer_market_type(text: str) -> str:
    lowered = text.lower()
    for market_type, hints in MARKET_HINTS.items():
        if any(hint in lowered for hint in hints):
            return market_type
    return "unknown"


def score_mapping(game: MlbGame, market: KalshiMarket) -> tuple[Decimal, str, dict[str, object]]:
    text = " ".join(
        value or ""
        for value in (
            market.title,
            market.subtitle,
            market.rules,
            market.yes_subtitle,
            market.no_subtitle,
            market.ticker,
            market.event_ticker,
        )
    )
    market_tokens = _tokens(text)
    score = Decimal("0")
    reasons: list[str] = []
    matched_teams = 0

    for team in (game.home_team, game.away_team):
        if _matches_team(team, market_tokens):
            matched_teams += 1
            score += Decimal("0.30")
            reasons.append(f"TEAM_MATCH:{team}")

    market_time = market.occurrence_datetime or market.close_time
    if market_time:
        minutes = abs((ensure_aware_utc(market_time) - ensure_aware_utc(game.scheduled_start)).total_seconds()) / 60
        if minutes <= 360:
            score += Decimal("0.25")
            reasons.append("START_TIME_PROXIMITY")
        elif minutes <= 24 * 60:
            score += Decimal("0.10")
            reasons.append("SAME_DAY_PROXIMITY")

    market_type = infer_market_type(text)
    if market_type != "unknown":
        score += Decimal("0.15")
        reasons.append(f"MARKET_TYPE:{market_type}")

    confidence = min(score, Decimal("0.9500")).quantize(Decimal("0.0001"))
    both_teams_matched = matched_teams == 2
    if both_teams_matched and confidence >= Decimal("0.60"):
        status = "candidate"
    elif confidence >= Decimal("0.25"):
        status = "needs_review"
    else:
        status = "rejected"

    metadata = {
        "market_type": market_type,
        "reasons": reasons,
        "home_team": game.home_team,
        "away_team": game.away_team,
        "matched_team_count": matched_teams,
        "market_ticker": market.ticker,
    }
    return confidence, status, metadata


def sync_market_mappings(session: Session) -> int:
    games = list(session.scalars(select(MlbGame)))
    markets = list(session.scalars(select(KalshiMarket)))
    count = 0

    for game in games:
        for market in markets:
            confidence, status, metadata = score_mapping(game, market)
            if status == "rejected":
                continue
            existing = session.scalar(
                select(MarketMapping).where(
                    MarketMapping.mlb_game_id == game.id,
                    MarketMapping.kalshi_market_id == market.id,
                )
            )
            row = existing or MarketMapping(mlb_game_id=game.id, kalshi_market_id=market.id)
            row.confidence = confidence
            row.mapping_status = status
            row.rationale = ", ".join(metadata["reasons"]) or "LOW INFORMATION MATCH"
            row.mapping_metadata = metadata
            session.add(row)
            count += 1

    session.commit()
    return count
