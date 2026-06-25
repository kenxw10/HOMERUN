from __future__ import annotations

from decimal import Decimal
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import KalshiMarket, MarketMapping, MlbGame
from app.services.kalshi_mlb_resolver import is_multivariate_market
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

    date_proximity_matched = False
    minutes_from_start: int | None = None
    market_time = market.occurrence_datetime or market.close_time
    if market_time:
        minutes = abs((ensure_aware_utc(market_time) - ensure_aware_utc(game.scheduled_start)).total_seconds()) / 60
        minutes_from_start = int(minutes)
        if minutes <= 360:
            date_proximity_matched = True
            score += Decimal("0.25")
            reasons.append("START_TIME_PROXIMITY")
        elif minutes <= 24 * 60:
            date_proximity_matched = True
            score += Decimal("0.10")
            reasons.append("SAME_DAY_PROXIMITY")
        else:
            reasons.append("DATE_PROXIMITY_MISMATCH")
    else:
        reasons.append("DATE_PROXIMITY_MISSING")

    market_type = infer_market_type(text)
    if market_type != "unknown":
        score += Decimal("0.15")
        reasons.append(f"MARKET_TYPE:{market_type}")

    confidence = min(score, Decimal("0.9500")).quantize(Decimal("0.0001"))
    both_teams_matched = matched_teams == 2
    if both_teams_matched and not date_proximity_matched:
        status = "rejected"
    elif both_teams_matched and confidence >= Decimal("0.60"):
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
        "date_proximity_matched": date_proximity_matched,
        "minutes_from_start": minutes_from_start,
        "market_ticker": market.ticker,
    }
    return confidence, status, metadata


def _disambiguate_market_scores(scored: list[dict[str, object]]) -> None:
    candidate_indexes: list[tuple[int, int]] = []
    for index, item in enumerate(scored):
        metadata = item["metadata"]
        if (
            item["status"] == "candidate"
            and isinstance(metadata, dict)
            and metadata.get("matched_team_count") == 2
            and metadata.get("date_proximity_matched") is True
            and isinstance(metadata.get("minutes_from_start"), int)
        ):
            candidate_indexes.append((index, metadata["minutes_from_start"]))

    if len(candidate_indexes) <= 1:
        return

    nearest_minutes = min(minutes for _, minutes in candidate_indexes)
    nearest_indexes = [index for index, minutes in candidate_indexes if minutes == nearest_minutes]
    unique_nearest_index = nearest_indexes[0] if len(nearest_indexes) == 1 else None

    for index, _ in candidate_indexes:
        if index == unique_nearest_index:
            continue

        metadata = dict(scored[index]["metadata"])
        reasons = list(metadata.get("reasons") or [])
        reasons.append("AMBIGUOUS_SAME_TEAM_GAME" if unique_nearest_index is None else "NON_NEAREST_SAME_TEAM_GAME")
        metadata["reasons"] = reasons
        scored[index]["metadata"] = metadata
        scored[index]["status"] = "needs_review"


def _is_multivariate_market_row(market: KalshiMarket) -> bool:
    payload = dict(market.raw_payload or {})
    payload.setdefault("ticker", market.ticker)
    payload.setdefault("event_ticker", market.event_ticker)
    return is_multivariate_market(payload)


def sync_market_mappings(session: Session) -> int:
    games = list(session.scalars(select(MlbGame)))
    markets = list(session.scalars(select(KalshiMarket)))
    count = 0

    for market in markets:
        if _is_multivariate_market_row(market):
            existing_rows = list(session.scalars(select(MarketMapping).where(MarketMapping.kalshi_market_id == market.id)))
            for row in existing_rows:
                row.mapping_status = "rejected_multivariate"
                row.validation_status = "rejected_multivariate"
                row.rationale = "REJECTED_MULTIVARIATE"
                row.mapping_metadata = {
                    **(row.mapping_metadata or {}),
                    "validation_notes": ["REJECTED_MULTIVARIATE"],
                }
                session.add(row)
                count += 1
            continue

        scored: list[dict[str, object]] = []
        for game in games:
            confidence, status, metadata = score_mapping(game, market)
            scored.append({"game": game, "confidence": confidence, "status": status, "metadata": metadata})

        _disambiguate_market_scores(scored)

        for item in scored:
            game = item["game"]
            confidence = item["confidence"]
            status = item["status"]
            metadata = item["metadata"]
            existing = session.scalar(
                select(MarketMapping).where(
                    MarketMapping.mlb_game_id == game.id,
                    MarketMapping.kalshi_market_id == market.id,
                )
            )
            if status == "rejected" and existing is None:
                continue
            row = existing or MarketMapping(mlb_game_id=game.id, kalshi_market_id=market.id)
            row.confidence = confidence
            row.mapping_status = status
            row.rationale = ", ".join(metadata["reasons"]) or "LOW INFORMATION MATCH"
            row.mapping_metadata = metadata
            session.add(row)
            count += 1

    session.commit()
    return count
