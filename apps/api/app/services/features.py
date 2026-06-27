from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from math import atan2, cos, radians, sin, sqrt
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    BullpenDailyFeature,
    InjurySnapshot,
    KalshiMarket,
    LineupSnapshot,
    MarketMapping,
    MlbGame,
    MlbFeatureSnapshot,
    ParkFactorSnapshot,
    PitcherDailyFeature,
    TeamDailyFeature,
    TeamRecentFeature,
    TravelScheduleFeature,
    WeatherSnapshot,
)
from app.services.contracts import (
    FIRST_FIVE_SPREAD,
    FIRST_FIVE_TOTAL,
    FIRST_FIVE_WINNER,
    FULL_GAME_SPREAD,
    FULL_GAME_TOTAL,
    FULL_GAME_WINNER,
    selected_team_from_ticker,
)
from app.services.http_json import HttpJsonError, get_json
from app.time_utils import ensure_aware_utc, get_dashboard_zone, today_eastern, utc_now

FEATURE_VERSION = "mature_mlb_features_v2"
MATURE_FEATURE_VERSION = FEATURE_VERSION
LEAGUE_AVG_FULL_GAME_RUNS = Decimal("4.35")
LEAGUE_AVG_FIRST_FIVE_RUNS = Decimal("2.15")
EARTH_RADIUS_MILES = Decimal("3958.8")
STATIC_SOURCE = "static_mlb_reference_v1"
MLB_STATS_SOURCE = "mlb_stats_api"
OPEN_METEO_SOURCE = "open_meteo"
DERIVED_SOURCE = "derived_homerun_v2"

CORE_MODULES = (
    "game_context",
    "market_context",
    "team_strength_prior",
    "offense_season",
    "offense_recent",
    "handedness_platoon",
    "starter_identity",
    "starter_season",
    "starter_recent",
    "starter_workload",
    "bullpen_season",
    "bullpen_recent_workload",
    "lineup",
    "injuries",
    "defense_catcher",
    "park_weather",
    "travel_schedule",
)

QUALITY_WEIGHTS: dict[str, dict[str, Decimal]] = {
    FULL_GAME_WINNER: {
        "game_context": Decimal("0.06"),
        "market_context": Decimal("0.06"),
        "team_strength_prior": Decimal("0.13"),
        "offense_season": Decimal("0.10"),
        "offense_recent": Decimal("0.08"),
        "handedness_platoon": Decimal("0.05"),
        "starter_identity": Decimal("0.08"),
        "starter_season": Decimal("0.08"),
        "starter_recent": Decimal("0.06"),
        "starter_workload": Decimal("0.05"),
        "bullpen_season": Decimal("0.08"),
        "bullpen_recent_workload": Decimal("0.05"),
        "lineup": Decimal("0.06"),
        "defense_catcher": Decimal("0.04"),
        "park_weather": Decimal("0.04"),
        "travel_schedule": Decimal("0.04"),
        "injuries": Decimal("0.04"),
    },
    FULL_GAME_SPREAD: {},
    FULL_GAME_TOTAL: {
        "game_context": Decimal("0.05"),
        "market_context": Decimal("0.06"),
        "team_strength_prior": Decimal("0.07"),
        "offense_season": Decimal("0.13"),
        "offense_recent": Decimal("0.10"),
        "handedness_platoon": Decimal("0.06"),
        "starter_identity": Decimal("0.08"),
        "starter_season": Decimal("0.10"),
        "starter_recent": Decimal("0.07"),
        "starter_workload": Decimal("0.06"),
        "bullpen_season": Decimal("0.09"),
        "bullpen_recent_workload": Decimal("0.06"),
        "lineup": Decimal("0.08"),
        "defense_catcher": Decimal("0.03"),
        "park_weather": Decimal("0.09"),
        "travel_schedule": Decimal("0.04"),
        "injuries": Decimal("0.03"),
    },
    FIRST_FIVE_WINNER: {
        "game_context": Decimal("0.06"),
        "market_context": Decimal("0.06"),
        "team_strength_prior": Decimal("0.08"),
        "offense_season": Decimal("0.12"),
        "offense_recent": Decimal("0.10"),
        "handedness_platoon": Decimal("0.08"),
        "starter_identity": Decimal("0.12"),
        "starter_season": Decimal("0.11"),
        "starter_recent": Decimal("0.09"),
        "starter_workload": Decimal("0.08"),
        "bullpen_season": Decimal("0.02"),
        "bullpen_recent_workload": Decimal("0.02"),
        "lineup": Decimal("0.10"),
        "defense_catcher": Decimal("0.03"),
        "park_weather": Decimal("0.04"),
        "travel_schedule": Decimal("0.06"),
        "injuries": Decimal("0.03"),
    },
    FIRST_FIVE_SPREAD: {},
    FIRST_FIVE_TOTAL: {},
}
QUALITY_WEIGHTS[FULL_GAME_SPREAD] = QUALITY_WEIGHTS[FULL_GAME_WINNER]
QUALITY_WEIGHTS[FIRST_FIVE_SPREAD] = QUALITY_WEIGHTS[FIRST_FIVE_WINNER]
QUALITY_WEIGHTS[FIRST_FIVE_TOTAL] = QUALITY_WEIGHTS[FIRST_FIVE_WINNER]

STADIUM_PROFILES: dict[str, dict[str, object]] = {
    "PNC Park": {
        "latitude": 40.4469,
        "longitude": -80.0057,
        "altitude_ft": 730,
        "roof_type": "open",
        "park_factor": 0.98,
        "run_factor": 0.99,
        "hr_factor": 0.95,
        "orientation_degrees": 115,
    },
    "T-Mobile Park": {
        "latitude": 47.5914,
        "longitude": -122.3325,
        "altitude_ft": 10,
        "roof_type": "retractable",
        "park_factor": 0.92,
        "run_factor": 0.91,
        "hr_factor": 0.90,
        "orientation_degrees": 130,
    },
    "Coors Field": {
        "latitude": 39.7561,
        "longitude": -104.9942,
        "altitude_ft": 5200,
        "roof_type": "open",
        "park_factor": 1.19,
        "run_factor": 1.22,
        "hr_factor": 1.15,
        "orientation_degrees": 35,
    },
    "Fenway Park": {
        "latitude": 42.3467,
        "longitude": -71.0972,
        "altitude_ft": 20,
        "roof_type": "open",
        "park_factor": 1.05,
        "run_factor": 1.06,
        "hr_factor": 0.97,
        "orientation_degrees": 45,
    },
    "Great American Ball Park": {
        "latitude": 39.0979,
        "longitude": -84.5082,
        "altitude_ft": 482,
        "roof_type": "open",
        "park_factor": 1.08,
        "run_factor": 1.07,
        "hr_factor": 1.18,
        "orientation_degrees": 130,
    },
}

TEAM_HOME_COORDINATES: dict[str, tuple[float, float]] = {
    "PIT": (40.4469, -80.0057),
    "SEA": (47.5914, -122.3325),
    "CIN": (39.0979, -84.5082),
    "BOS": (42.3467, -71.0972),
    "COL": (39.7561, -104.9942),
}


def _decimal(value: object, places: str = "0.0001") -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(Decimal(places))
    except Exception:
        return None


def _int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except Exception:
        return None


def _float(value: Decimal | float | int | None) -> float | None:
    return float(value) if value is not None else None


def _module(
    component: str,
    status: str,
    reason: str,
    *,
    captured_at: datetime,
    source: str,
    confidence: Decimal | str = Decimal("0"),
    completeness: Decimal | str = Decimal("0"),
    stale: bool = False,
    values: dict[str, object] | None = None,
) -> dict[str, object]:
    confidence_decimal = Decimal(str(confidence)).quantize(Decimal("0.0001"))
    completeness_decimal = Decimal(str(completeness)).quantize(Decimal("0.0001"))
    payload: dict[str, object] = {
        "component": component,
        "source_status": status,
        "source": source,
        "captured_at": captured_at.isoformat(),
        "stale": stale,
        "confidence": float(confidence_decimal),
        "completeness": float(completeness_decimal),
        "reason": reason,
    }
    if values:
        payload.update(values)
    return payload


def _missing(component: str, reason: str, captured_at: datetime) -> dict[str, object]:
    return _module(component, "missing", reason, captured_at=captured_at, source="not_configured")


def _partial(
    component: str,
    reason: str,
    *,
    captured_at: datetime,
    source: str,
    confidence: Decimal | str = Decimal("0.35"),
    completeness: Decimal | str = Decimal("0.35"),
    values: dict[str, object] | None = None,
) -> dict[str, object]:
    return _module(
        component,
        "partial",
        reason,
        captured_at=captured_at,
        source=source,
        confidence=confidence,
        completeness=completeness,
        values=values,
    )


def _available(
    component: str,
    values: dict[str, object],
    *,
    captured_at: datetime,
    source: str,
    confidence: Decimal | str = Decimal("0.80"),
    completeness: Decimal | str = Decimal("0.80"),
) -> dict[str, object]:
    return _module(
        component,
        "available",
        "source populated",
        captured_at=captured_at,
        source=source,
        confidence=confidence,
        completeness=completeness,
        values=values,
    )


def _team_payload(game: MlbGame, side: str) -> dict[str, Any]:
    raw = game.raw_payload or {}
    teams = raw.get("teams") if isinstance(raw, dict) else None
    team = teams.get(side) if isinstance(teams, dict) else None
    return team if isinstance(team, dict) else {}


def _venue_payload(game: MlbGame) -> dict[str, Any]:
    raw = game.raw_payload or {}
    venue = raw.get("venue") if isinstance(raw, dict) else None
    return venue if isinstance(venue, dict) else {}


def _venue_name(game: MlbGame) -> str | None:
    venue = _venue_payload(game)
    name = venue.get("name")
    return str(name) if name else None


def _venue(game: MlbGame, captured_at: datetime) -> dict[str, object]:
    venue = _venue_payload(game)
    if venue.get("name"):
        return _available(
            "venue",
            {"id": venue.get("id"), "name": venue.get("name")},
            captured_at=captured_at,
            source=MLB_STATS_SOURCE,
        )
    return _missing("venue", "MLB payload did not include venue", captured_at)


def _selected_code(game: MlbGame, market: KalshiMarket, mapping: MarketMapping) -> str | None:
    selected = mapping.selection_code or market.selection_code or selected_team_from_ticker(market.ticker)
    return selected.upper() if selected else None


def _time_bucket(minutes_to_start: int | None) -> str | None:
    if minutes_to_start is None:
        return None
    if minutes_to_start >= 24 * 60:
        return "24H_PLUS"
    if minutes_to_start >= 12 * 60:
        return "12H"
    if minutes_to_start >= 90:
        return "90M"
    if minutes_to_start >= 15:
        return "15M"
    if minutes_to_start > 0:
        return "5M"
    return "POST_START"


def _day_night(game: MlbGame) -> str:
    local = ensure_aware_utc(game.scheduled_start).astimezone(get_dashboard_zone())
    return "day" if time(11, 0) <= local.time() < time(18, 0) else "night"


def _team_record(game: MlbGame, side: str) -> dict[str, object]:
    team = _team_payload(game, side)
    record = team.get("leagueRecord")
    if not isinstance(record, dict):
        return {}
    wins = _int(record.get("wins"))
    losses = _int(record.get("losses"))
    pct = _decimal(record.get("pct"))
    if pct is None and wins is not None and losses is not None and wins + losses > 0:
        pct = (Decimal(wins) / Decimal(wins + losses)).quantize(Decimal("0.0001"))
    return {"wins": wins, "losses": losses, "win_pct": _float(pct)}


def _pythagorean(runs_for: Decimal | None, runs_against: Decimal | None) -> Decimal | None:
    if runs_for is None or runs_against is None:
        return None
    if runs_for <= 0 and runs_against <= 0:
        return None
    exponent = Decimal("1.83")
    scored = Decimal(str(float(runs_for) ** float(exponent)))
    allowed = Decimal(str(float(runs_against) ** float(exponent)))
    return (scored / (scored + allowed)).quantize(Decimal("0.0001"))


def _team_games(
    session: Session,
    game: MlbGame,
    team_code: str,
    *,
    days: int | None = None,
) -> list[MlbGame]:
    start = ensure_aware_utc(game.scheduled_start)
    statement = (
        select(MlbGame)
        .where(MlbGame.id != game.id)
        .where(MlbGame.scheduled_start < start)
        .where((MlbGame.home_abbreviation == team_code) | (MlbGame.away_abbreviation == team_code))
        .where(MlbGame.home_score.is_not(None))
        .where(MlbGame.away_score.is_not(None))
        .order_by(MlbGame.scheduled_start.desc())
    )
    if days is not None:
        statement = statement.where(MlbGame.scheduled_start >= start - timedelta(days=days))
    return list(session.scalars(statement.limit(162)))


def _team_run_totals(games: list[MlbGame], team_code: str) -> tuple[Decimal | None, Decimal | None, int]:
    scored = Decimal("0")
    allowed = Decimal("0")
    count = 0
    for previous in games:
        if previous.home_score is None or previous.away_score is None:
            continue
        if previous.home_abbreviation == team_code:
            scored += Decimal(previous.home_score)
            allowed += Decimal(previous.away_score)
        elif previous.away_abbreviation == team_code:
            scored += Decimal(previous.away_score)
            allowed += Decimal(previous.home_score)
        else:
            continue
        count += 1
    if count == 0:
        return None, None, 0
    return (scored / count).quantize(Decimal("0.0001")), (allowed / count).quantize(Decimal("0.0001")), count


def _rest_days(session: Session | None, game: MlbGame, team_code: str | None) -> int | None:
    if session is None or game.id is None or not team_code:
        return None
    previous = _team_games(session, game, team_code, days=14)
    if not previous:
        return None
    delta = ensure_aware_utc(game.scheduled_start).date() - ensure_aware_utc(previous[0].scheduled_start).date()
    return max(delta.days - 1, 0)


def _distance_between_points(
    first: tuple[float, float] | None,
    second: tuple[float, float] | None,
) -> float | None:
    if first is None or second is None:
        return None
    lat1, lon1 = first
    lat2, lon2 = second
    d_lat = radians(lat2 - lat1)
    d_lon = radians(lon2 - lon1)
    a = sin(d_lat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(d_lon / 2) ** 2
    return float(EARTH_RADIUS_MILES * Decimal(str(2 * atan2(sqrt(a), sqrt(1 - a)))))


def _team_location_for_game(game: MlbGame, team_code: str) -> tuple[float, float] | None:
    venue_name = _venue_name(game)
    profile = STADIUM_PROFILES.get(venue_name or "")
    if profile:
        return float(profile["latitude"]), float(profile["longitude"])
    if team_code == game.home_abbreviation:
        return TEAM_HOME_COORDINATES.get(team_code)
    if team_code == game.away_abbreviation:
        return None
    return TEAM_HOME_COORDINATES.get(team_code)


def parse_starting_lineup_from_game_payload(payload: dict[str, object], side: str) -> list[dict[str, object]]:
    live_data = payload.get("liveData") if isinstance(payload, dict) else None
    boxscore = live_data.get("boxscore") if isinstance(live_data, dict) else None
    teams = boxscore.get("teams") if isinstance(boxscore, dict) else None
    team = teams.get(side) if isinstance(teams, dict) else None
    if not isinstance(team, dict):
        return []
    batters = team.get("batters")
    players = team.get("players")
    if not isinstance(batters, list) or not isinstance(players, dict):
        return []

    starters: list[dict[str, object]] = []
    for person_id in batters:
        player = players.get(f"ID{person_id}") or players.get(str(person_id))
        if not isinstance(player, dict):
            continue
        order_value = player.get("battingOrder")
        try:
            batting_order = int(str(order_value))
        except Exception:
            continue
        if batting_order not in {100, 200, 300, 400, 500, 600, 700, 800, 900}:
            continue
        person = player.get("person") if isinstance(player.get("person"), dict) else {}
        batting_side = player.get("batSide") if isinstance(player.get("batSide"), dict) else {}
        primary_position = (
            player.get("position") if isinstance(player.get("position"), dict) else {}
        )
        starters.append(
            {
                "person_id": str(person.get("id") or person_id),
                "name": person.get("fullName") or player.get("fullName"),
                "batting_order": batting_order,
                "batting_order_slot": batting_order // 100,
                "bat_side": batting_side.get("code") or batting_side.get("description"),
                "position": primary_position.get("abbreviation") or primary_position.get("code"),
            }
        )
    return sorted(starters, key=lambda item: int(item["batting_order"]))


def probable_pitcher_from_payload(payload: dict[str, object], side: str) -> dict[str, object] | None:
    teams = payload.get("teams") if isinstance(payload, dict) else None
    team = teams.get(side) if isinstance(teams, dict) else None
    pitcher = team.get("probablePitcher") if isinstance(team, dict) else None
    if isinstance(pitcher, dict) and pitcher.get("id"):
        return {
            "id": str(pitcher.get("id")),
            "name": pitcher.get("fullName"),
            "handedness": None,
            "source_path": f"schedule.teams.{side}.probablePitcher",
        }

    live_data = payload.get("liveData") if isinstance(payload, dict) else None
    boxscore = live_data.get("boxscore") if isinstance(live_data, dict) else None
    box_teams = boxscore.get("teams") if isinstance(boxscore, dict) else None
    box_team = box_teams.get(side) if isinstance(box_teams, dict) else None
    pitchers = box_team.get("pitchers") if isinstance(box_team, dict) else None
    players = box_team.get("players") if isinstance(box_team, dict) else None
    if isinstance(pitchers, list) and pitchers and isinstance(players, dict):
        pitcher_id = pitchers[0]
        player = players.get(f"ID{pitcher_id}") or players.get(str(pitcher_id))
        if isinstance(player, dict):
            person = player.get("person") if isinstance(player.get("person"), dict) else {}
            pitch_hand = player.get("pitchHand") if isinstance(player.get("pitchHand"), dict) else {}
            return {
                "id": str(person.get("id") or pitcher_id),
                "name": person.get("fullName") or player.get("fullName"),
                "handedness": pitch_hand.get("code") or pitch_hand.get("description"),
                "source_path": f"game.liveData.boxscore.teams.{side}.pitchers[0]",
            }
    return None


def _game_endpoint_url(game_pk: str) -> str:
    base = get_settings().mlb_stats_base_url.rstrip("/")
    return f"{base}/game/{game_pk}/feed/live"


def fetch_game_endpoint(game_pk: str) -> dict[str, object]:
    return get_json(_game_endpoint_url(game_pk), params={})


def _cached_team_daily(session: Session | None, day: date, team_code: str | None) -> TeamDailyFeature | None:
    if session is None or not team_code:
        return None
    return session.scalar(
        select(TeamDailyFeature)
        .where(TeamDailyFeature.target_date == day)
        .where(TeamDailyFeature.team_code == team_code)
        .order_by(TeamDailyFeature.updated_at.desc())
        .limit(1)
    )


def _cached_team_recent(
    session: Session | None,
    day: date,
    team_code: str | None,
    window_days: int = 14,
) -> TeamRecentFeature | None:
    if session is None or not team_code:
        return None
    return session.scalar(
        select(TeamRecentFeature)
        .where(TeamRecentFeature.target_date == day)
        .where(TeamRecentFeature.team_code == team_code)
        .where(TeamRecentFeature.window_days == window_days)
        .order_by(TeamRecentFeature.updated_at.desc())
        .limit(1)
    )


def _cached_lineup(
    session: Session | None,
    game: MlbGame,
    team_code: str | None,
) -> LineupSnapshot | None:
    if session is None or game.id is None or not team_code:
        return None
    return session.scalar(
        select(LineupSnapshot)
        .where(LineupSnapshot.mlb_game_id == game.id)
        .where(LineupSnapshot.team_code == team_code)
        .order_by(LineupSnapshot.updated_at.desc())
        .limit(1)
    )


def _cached_single(
    session: Session | None,
    model,
    *criteria,
):
    if session is None:
        return None
    statement = select(model)
    for item in criteria:
        statement = statement.where(item)
    return session.scalar(statement.order_by(model.updated_at.desc()).limit(1))


def _team_daily_module(
    row: TeamDailyFeature | None,
    side: str,
    game: MlbGame,
    captured_at: datetime,
) -> dict[str, object]:
    if row is not None:
        return _module(
            f"{side}_team_daily",
            row.source_status,
            "cached daily team feature",
            captured_at=row.captured_at,
            source=row.source,
            confidence=row.confidence or Decimal("0"),
            completeness=row.completeness or Decimal("0"),
            stale=row.stale,
            values=row.features,
        )
    record = _team_record(game, side)
    if record:
        return _partial(
            f"{side}_team_daily",
            "schedule record present but no derived daily team cache",
            captured_at=captured_at,
            source=MLB_STATS_SOURCE,
            confidence="0.35",
            completeness="0.25",
            values=record,
        )
    return _missing(f"{side}_team_daily", "daily team cache missing", captured_at)


def _team_recent_module(row: TeamRecentFeature | None, side: str, captured_at: datetime) -> dict[str, object]:
    if row is not None:
        return _module(
            f"{side}_team_recent",
            row.source_status,
            "cached recent team feature",
            captured_at=row.captured_at,
            source=row.source,
            confidence=row.confidence or Decimal("0"),
            completeness=row.completeness or Decimal("0"),
            stale=row.stale,
            values={**row.features, "sample_size": row.sample_size, "window_days": row.window_days},
        )
    return _missing(f"{side}_team_recent", "recent offense cache missing", captured_at)


def _pitcher_module(
    row: PitcherDailyFeature | None,
    identity: dict[str, object] | None,
    side: str,
    captured_at: datetime,
) -> dict[str, object]:
    if row is not None:
        return _module(
            f"{side}_starter",
            row.source_status,
            "cached pitcher feature",
            captured_at=row.captured_at,
            source=row.source,
            confidence=row.confidence or Decimal("0"),
            completeness=row.completeness or Decimal("0"),
            stale=row.stale,
            values={**row.features, "pitcher_id": row.pitcher_id, "pitcher_name": row.pitcher_name},
        )
    if identity:
        return _partial(
            f"{side}_starter",
            "probable starter identified but advanced stats unavailable",
            captured_at=captured_at,
            source=MLB_STATS_SOURCE,
            confidence="0.45",
            completeness="0.25",
            values=identity,
        )
    return _missing(f"{side}_starter", "probable starter unavailable", captured_at)


def _lineup_module(row: LineupSnapshot | None, side: str, captured_at: datetime) -> dict[str, object]:
    if row is not None:
        return _module(
            f"{side}_lineup",
            row.source_status,
            "cached lineup snapshot",
            captured_at=row.captured_at,
            source=row.source,
            confidence=row.confidence or Decimal("0"),
            completeness=row.completeness or Decimal("0"),
            stale=row.stale,
            values={**row.features, "confirmed": row.confirmed},
        )
    return _missing(f"{side}_lineup", "confirmed or projected lineup cache missing", captured_at)


def _source_statuses(features: dict[str, object]) -> dict[str, object]:
    statuses: dict[str, object] = {}
    for key, value in features.items():
        if isinstance(value, dict) and "source_status" in value:
            statuses[key] = value["source_status"]
            continue
        if isinstance(value, dict):
            nested = {
                nested_key: nested_value.get("source_status")
                for nested_key, nested_value in value.items()
                if isinstance(nested_value, dict) and "source_status" in nested_value
            }
            if nested:
                statuses[key] = nested
    return statuses


def _status_score(module: dict[str, object]) -> Decimal:
    status = module.get("source_status")
    completeness = _decimal(module.get("completeness")) or Decimal("0")
    if status == "available":
        return max(min(completeness, Decimal("1.0")), Decimal("0"))
    if status == "partial":
        return max(min(completeness, Decimal("0.5")), Decimal("0"))
    return Decimal("0")


def _nested_module_score(value: object) -> Decimal:
    if isinstance(value, dict) and "source_status" in value:
        return _status_score(value)
    if not isinstance(value, dict):
        return Decimal("0")
    scores = [_status_score(item) for item in value.values() if isinstance(item, dict)]
    if not scores:
        return Decimal("0")
    return sum(scores) / Decimal(len(scores))


def _quality_score(
    features: dict[str, object],
    market_family: str | None,
    minutes_to_start: int | None,
) -> tuple[Decimal, dict[str, object]]:
    weights = QUALITY_WEIGHTS.get(market_family or FULL_GAME_WINNER, QUALITY_WEIGHTS[FULL_GAME_WINNER])
    total_weight = sum(weights.values())
    weighted = Decimal("0")
    module_scores: dict[str, float] = {}
    for module_name, weight in weights.items():
        score = _nested_module_score(features.get(module_name))
        module_scores[module_name] = float(score.quantize(Decimal("0.0001")))
        weighted += score * weight
    quality = weighted / total_weight if total_weight else Decimal("0")
    reasons: list[str] = []

    starter_identity = features.get("starter_identity")
    home_starter = starter_identity.get("home") if isinstance(starter_identity, dict) else None
    away_starter = starter_identity.get("away") if isinstance(starter_identity, dict) else None
    if (
        isinstance(home_starter, dict)
        and isinstance(away_starter, dict)
        and home_starter.get("source_status") != "available"
        and away_starter.get("source_status") != "available"
    ):
        quality = min(quality, Decimal("0.6000"))
        reasons.append("CAP_BOTH_STARTERS_MISSING")

    if (
        _nested_module_score(features.get("offense_season")) == Decimal("0")
        and _nested_module_score(features.get("offense_recent")) == Decimal("0")
    ):
        quality = min(quality, Decimal("0.6500"))
        reasons.append("CAP_OFFENSE_SEASON_AND_RECENT_MISSING")

    if minutes_to_start is not None and 0 < minutes_to_start <= 90:
        if _nested_module_score(features.get("lineup")) == Decimal("0"):
            quality = min(quality, Decimal("0.7000"))
            reasons.append("CAP_LINEUP_MISSING_WITHIN_90M")

    quality = min(max(quality, Decimal("0.0000")), Decimal("1.0000")).quantize(Decimal("0.0001"))
    return quality, {
        "score": float(quality),
        "module_scores": module_scores,
        "data_quality_reason": reasons,
        "weight_profile": market_family or FULL_GAME_WINNER,
        "source_statuses": _source_statuses(features),
    }


def build_feature_snapshot(
    game: MlbGame,
    market: KalshiMarket,
    mapping: MarketMapping,
    *,
    session: Session | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    captured_at = now or utc_now()
    selected = _selected_code(game, market, mapping)
    home_code = (game.home_abbreviation or "").upper()
    away_code = (game.away_abbreviation or "").upper()
    scheduled_start = ensure_aware_utc(game.scheduled_start)
    target_date = scheduled_start.astimezone(get_dashboard_zone()).date()
    minutes_to_start = int((scheduled_start - captured_at).total_seconds() / 60)
    market_family = mapping.market_family or market.market_family or mapping.market_type or market.market_type

    home_daily = _cached_team_daily(session, target_date, home_code)
    away_daily = _cached_team_daily(session, target_date, away_code)
    home_recent = _cached_team_recent(session, target_date, home_code)
    away_recent = _cached_team_recent(session, target_date, away_code)
    home_identity = probable_pitcher_from_payload(game.raw_payload or {}, "home")
    away_identity = probable_pitcher_from_payload(game.raw_payload or {}, "away")
    home_pitcher = _cached_pitcher(session, target_date, home_code, home_identity)
    away_pitcher = _cached_pitcher(session, target_date, away_code, away_identity)
    home_lineup = _cached_lineup(session, game, home_code)
    away_lineup = _cached_lineup(session, game, away_code)
    weather = _cached_single(session, WeatherSnapshot, WeatherSnapshot.mlb_game_id == game.id)
    park = _cached_single(session, ParkFactorSnapshot, ParkFactorSnapshot.venue_name == (_venue_name(game) or ""))
    home_travel = _cached_single(
        session,
        TravelScheduleFeature,
        TravelScheduleFeature.mlb_game_id == game.id,
        TravelScheduleFeature.team_code == home_code,
    )
    away_travel = _cached_single(
        session,
        TravelScheduleFeature,
        TravelScheduleFeature.mlb_game_id == game.id,
        TravelScheduleFeature.team_code == away_code,
    )
    home_bullpen = _cached_single(
        session,
        BullpenDailyFeature,
        BullpenDailyFeature.target_date == target_date,
        BullpenDailyFeature.team_code == home_code,
    )
    away_bullpen = _cached_single(
        session,
        BullpenDailyFeature,
        BullpenDailyFeature.target_date == target_date,
        BullpenDailyFeature.team_code == away_code,
    )
    home_injury = _cached_single(
        session,
        InjurySnapshot,
        InjurySnapshot.target_date == target_date,
        InjurySnapshot.team_code == home_code,
    )
    away_injury = _cached_single(
        session,
        InjurySnapshot,
        InjurySnapshot.target_date == target_date,
        InjurySnapshot.team_code == away_code,
    )

    features: dict[str, object] = {
        "feature_version": FEATURE_VERSION,
        "captured_at": captured_at.isoformat(),
        "game_context": _available(
            "game_context",
            {
                "external_game_id": game.external_game_id,
                "scheduled_start_utc": scheduled_start.isoformat(),
                "scheduled_start_eastern": scheduled_start.astimezone(get_dashboard_zone()).isoformat(),
                "home_team": game.home_team,
                "away_team": game.away_team,
                "home_abbreviation": home_code,
                "away_abbreviation": away_code,
                "venue": _venue(game, captured_at),
                "game_status": game.status,
                "day_night": _day_night(game),
                "doubleheader": _doubleheader_flag(game),
                "series_game_number": _series_game_number(game),
                "game_number_in_doubleheader": _game_number_in_doubleheader(game),
                "start_time_bucket": _time_bucket(minutes_to_start),
                "pregame_live_final": _game_phase(game),
            },
            captured_at=captured_at,
            source=MLB_STATS_SOURCE,
            confidence="0.90",
            completeness="0.90",
        ),
        "market_context": _available(
            "market_context",
            {
                "market_family": market_family,
                "ticker": market.ticker,
                "event_ticker": market.event_ticker,
                "side": "yes",
                "line_value": _float(mapping.line_value if mapping.line_value is not None else market.line_value),
                "selection_code": selected,
                "over_under_side": mapping.over_under_side or market.over_under_side,
                "inning_scope": mapping.inning_scope or market.inning_scope,
                "executable_price": None,
                "executable_price_source": None,
                "yes_bid": _float(market.yes_bid),
                "yes_ask": _float(market.yes_ask),
                "no_bid": _float(market.no_bid),
                "no_ask": _float(market.no_ask),
                "best_yes_bid": _float(market.best_yes_bid),
                "best_no_bid": _float(market.best_no_bid),
                "implied_yes_ask": _float(market.implied_yes_ask),
                "implied_no_ask": _float(market.implied_no_ask),
                "last_mark_timestamp": market.market_price_updated_at.isoformat()
                if market.market_price_updated_at
                else None,
                "time_to_start_minutes": minutes_to_start,
                "time_bucket": _time_bucket(minutes_to_start),
                "fee_estimate": 0.0,
                "mapping_confidence": _float(mapping.confidence),
                "settlement_rule_status": mapping.settlement_rule_status or market.settlement_rule_status,
                "price_freshness": None,
            },
            captured_at=captured_at,
            source="kalshi_public_market_data",
            confidence="0.90",
            completeness="0.85",
        ),
        "team_strength_prior": _team_strength_module(
            home_daily,
            away_daily,
            game,
            captured_at,
        ),
        "offense_season": {
            "home": _team_daily_module(home_daily, "home", game, captured_at),
            "away": _team_daily_module(away_daily, "away", game, captured_at),
        },
        "offense_recent": {
            "home": _team_recent_module(home_recent, "home", captured_at),
            "away": _team_recent_module(away_recent, "away", captured_at),
        },
        "handedness_platoon": _handedness_module(
            home_lineup,
            away_lineup,
            home_identity,
            away_identity,
            captured_at,
        ),
        "starter_identity": {
            "home": _starter_identity_module(home_identity, "home", captured_at),
            "away": _starter_identity_module(away_identity, "away", captured_at),
        },
        "starter_season": {
            "home": _pitcher_module(home_pitcher, home_identity, "home", captured_at),
            "away": _pitcher_module(away_pitcher, away_identity, "away", captured_at),
        },
        "starter_recent": {
            "home": _pitcher_recent_module(home_pitcher, "home", captured_at),
            "away": _pitcher_recent_module(away_pitcher, "away", captured_at),
        },
        "starter_workload": {
            "home": _pitcher_workload_module(home_pitcher, home_bullpen, "home", captured_at),
            "away": _pitcher_workload_module(away_pitcher, away_bullpen, "away", captured_at),
        },
        "bullpen_season": {
            "home": _bullpen_module(home_bullpen, "home", captured_at),
            "away": _bullpen_module(away_bullpen, "away", captured_at),
        },
        "bullpen_recent_workload": {
            "home": _bullpen_recent_module(home_bullpen, "home", captured_at),
            "away": _bullpen_recent_module(away_bullpen, "away", captured_at),
        },
        "lineup": {
            "home": _lineup_module(home_lineup, "home", captured_at),
            "away": _lineup_module(away_lineup, "away", captured_at),
        },
        "injuries": {
            "home": _injury_module(home_injury, "home", captured_at),
            "away": _injury_module(away_injury, "away", captured_at),
        },
        "defense_catcher": _defense_catcher_module(home_lineup, away_lineup, captured_at),
        "park_weather": _park_weather_module(park, weather, game, captured_at),
        "travel_schedule": {
            "home": _travel_module(home_travel, "home", captured_at),
            "away": _travel_module(away_travel, "away", captured_at),
        },
    }
    data_quality, quality_summary = _quality_score(features, str(market_family or ""), minutes_to_start)
    features["data_quality"] = float(data_quality)
    features["data_quality_summary"] = {
        **quality_summary,
        "context": "pregame" if minutes_to_start > 0 else "post_start",
    }
    features["data_quality_reason"] = quality_summary["data_quality_reason"]
    features["source_statuses"] = _source_statuses(features)
    return features


def _cached_pitcher(
    session: Session | None,
    day: date,
    team_code: str,
    identity: dict[str, object] | None,
) -> PitcherDailyFeature | None:
    if session is None or not identity:
        return None
    pitcher_id = identity.get("id")
    if not pitcher_id:
        return None
    return session.scalar(
        select(PitcherDailyFeature)
        .where(PitcherDailyFeature.target_date == day)
        .where(PitcherDailyFeature.team_code == team_code)
        .where(PitcherDailyFeature.pitcher_id == str(pitcher_id))
        .order_by(PitcherDailyFeature.updated_at.desc())
        .limit(1)
    )


def _doubleheader_flag(game: MlbGame) -> bool:
    raw = game.raw_payload or {}
    return bool(raw.get("doubleHeader")) if isinstance(raw, dict) else False


def _series_game_number(game: MlbGame) -> object:
    raw = game.raw_payload or {}
    return raw.get("seriesGameNumber") if isinstance(raw, dict) else None


def _game_number_in_doubleheader(game: MlbGame) -> object:
    raw = game.raw_payload or {}
    return raw.get("gameNumber") if isinstance(raw, dict) else None


def _game_phase(game: MlbGame) -> str:
    status = game.status.strip().lower()
    if any(token in status for token in ("final", "completed", "game over")):
        return "final"
    if any(token in status for token in ("in progress", "live", "warmup", "delayed")):
        return "live"
    return "pregame"


def _team_strength_module(
    home_daily: TeamDailyFeature | None,
    away_daily: TeamDailyFeature | None,
    game: MlbGame,
    captured_at: datetime,
) -> dict[str, object]:
    home = _team_daily_module(home_daily, "home", game, captured_at)
    away = _team_daily_module(away_daily, "away", game, captured_at)
    status = "available" if home.get("source_status") == away.get("source_status") == "available" else "partial"
    if home.get("source_status") == "missing" and away.get("source_status") == "missing":
        status = "missing"
    return _module(
        "team_strength_prior",
        status,
        "uses derived run differential and shrinkage when cached data exists",
        captured_at=captured_at,
        source=DERIVED_SOURCE,
        confidence="0.65" if status == "available" else "0.30",
        completeness="0.65" if status == "available" else "0.30",
        values={
            "home": home,
            "away": away,
            "league_average_full_game_runs": float(LEAGUE_AVG_FULL_GAME_RUNS),
            "league_average_first_five_runs": float(LEAGUE_AVG_FIRST_FIVE_RUNS),
            "early_season_shrinkage": 0.65,
        },
    )


def _starter_identity_module(
    identity: dict[str, object] | None,
    side: str,
    captured_at: datetime,
) -> dict[str, object]:
    if identity:
        return _available(
            f"{side}_starter_identity",
            identity,
            captured_at=captured_at,
            source=MLB_STATS_SOURCE,
            confidence="0.70",
            completeness="0.60",
        )
    return _missing(f"{side}_starter_identity", "probable pitcher not present", captured_at)


def _pitcher_recent_module(
    row: PitcherDailyFeature | None,
    side: str,
    captured_at: datetime,
) -> dict[str, object]:
    if row is not None:
        recent = row.features.get("recent") if isinstance(row.features, dict) else None
        if isinstance(recent, dict):
            return _module(
                f"{side}_starter_recent",
                row.source_status,
                "cached pitcher recent feature",
                captured_at=row.captured_at,
                source=row.source,
                confidence=row.confidence or Decimal("0"),
                completeness=row.completeness or Decimal("0"),
                stale=row.stale,
                values=recent,
            )
    return _missing(f"{side}_starter_recent", "starter recent cache missing", captured_at)


def _pitcher_workload_module(
    pitcher_row: PitcherDailyFeature | None,
    bullpen_row: BullpenDailyFeature | None,
    side: str,
    captured_at: datetime,
) -> dict[str, object]:
    if pitcher_row is not None:
        workload = pitcher_row.features.get("workload") if isinstance(pitcher_row.features, dict) else None
        if isinstance(workload, dict):
            return _module(
                f"{side}_starter_workload",
                pitcher_row.source_status,
                "cached pitcher workload feature",
                captured_at=pitcher_row.captured_at,
                source=pitcher_row.source,
                confidence=pitcher_row.confidence or Decimal("0"),
                completeness=pitcher_row.completeness or Decimal("0"),
                stale=pitcher_row.stale,
                values=workload,
            )
    expected_bullpen = None
    if bullpen_row is not None:
        expected_bullpen = bullpen_row.features.get("expected_bullpen_innings")
    return _partial(
        f"{side}_starter_workload",
        "starter workload unavailable; using bullpen fallback only",
        captured_at=captured_at,
        source=DERIVED_SOURCE,
        confidence="0.25",
        completeness="0.20",
        values={"expected_bullpen_innings": expected_bullpen},
    )


def _bullpen_module(
    row: BullpenDailyFeature | None,
    side: str,
    captured_at: datetime,
) -> dict[str, object]:
    if row is not None:
        return _module(
            f"{side}_bullpen_season",
            row.source_status,
            "cached bullpen feature",
            captured_at=row.captured_at,
            source=row.source,
            confidence=row.confidence or Decimal("0"),
            completeness=row.completeness or Decimal("0"),
            stale=row.stale,
            values=row.features,
        )
    return _missing(f"{side}_bullpen_season", "bullpen cache missing", captured_at)


def _bullpen_recent_module(
    row: BullpenDailyFeature | None,
    side: str,
    captured_at: datetime,
) -> dict[str, object]:
    if row is not None:
        recent = row.features.get("recent_workload") if isinstance(row.features, dict) else None
        if isinstance(recent, dict):
            return _module(
                f"{side}_bullpen_recent_workload",
                row.source_status,
                "cached bullpen workload feature",
                captured_at=row.captured_at,
                source=row.source,
                confidence=row.confidence or Decimal("0"),
                completeness=row.completeness or Decimal("0"),
                stale=row.stale,
                values=recent,
            )
    return _missing(f"{side}_bullpen_recent_workload", "bullpen workload cache missing", captured_at)


def _handedness_module(
    home_lineup: LineupSnapshot | None,
    away_lineup: LineupSnapshot | None,
    home_starter: dict[str, object] | None,
    away_starter: dict[str, object] | None,
    captured_at: datetime,
) -> dict[str, object]:
    home_mix = home_lineup.features.get("handedness_mix") if home_lineup else None
    away_mix = away_lineup.features.get("handedness_mix") if away_lineup else None
    values = {
        "home_lineup_handedness_mix": home_mix,
        "away_lineup_handedness_mix": away_mix,
        "home_opposing_starter_handedness": away_starter.get("handedness") if away_starter else None,
        "away_opposing_starter_handedness": home_starter.get("handedness") if home_starter else None,
    }
    if home_mix or away_mix or home_starter or away_starter:
        return _partial(
            "handedness_platoon",
            "handedness partially inferred from lineup or starter identity",
            captured_at=captured_at,
            source=DERIVED_SOURCE,
            confidence="0.45",
            completeness="0.35",
            values=values,
        )
    return _missing("handedness_platoon", "lineup and starter handedness missing", captured_at)


def _injury_module(row: InjurySnapshot | None, side: str, captured_at: datetime) -> dict[str, object]:
    if row is not None:
        return _module(
            f"{side}_injuries",
            row.source_status,
            "cached injury snapshot",
            captured_at=row.captured_at,
            source=row.source,
            confidence=row.confidence or Decimal("0"),
            completeness=row.completeness or Decimal("0"),
            stale=row.stale,
            values=row.features,
        )
    return _missing(
        f"{side}_injuries",
        "no reliable no-key injury source configured; optional provider absent",
        captured_at,
    )


def _defense_catcher_module(
    home_lineup: LineupSnapshot | None,
    away_lineup: LineupSnapshot | None,
    captured_at: datetime,
) -> dict[str, object]:
    home_catcher = _catcher_from_lineup(home_lineup)
    away_catcher = _catcher_from_lineup(away_lineup)
    if home_catcher or away_catcher:
        return _partial(
            "defense_catcher",
            "catcher starts inferred from lineup; advanced catcher metrics unavailable",
            captured_at=captured_at,
            source=DERIVED_SOURCE,
            confidence="0.35",
            completeness="0.25",
            values={
                "home_catcher": home_catcher,
                "away_catcher": away_catcher,
                "team_defense_proxy": None,
                "outs_above_average": None,
                "catcher_framing": None,
                "umpire": "excluded",
            },
        )
    return _missing("defense_catcher", "defense and catcher metrics unavailable; umpire excluded", captured_at)


def _catcher_from_lineup(row: LineupSnapshot | None) -> dict[str, object] | None:
    if row is None:
        return None
    starters = row.features.get("starters") if isinstance(row.features, dict) else None
    if not isinstance(starters, list):
        return None
    for starter in starters:
        if isinstance(starter, dict) and starter.get("position") == "C":
            return starter
    return None


def _park_weather_module(
    park: ParkFactorSnapshot | None,
    weather: WeatherSnapshot | None,
    game: MlbGame,
    captured_at: datetime,
) -> dict[str, object]:
    venue_name = _venue_name(game)
    static_profile = STADIUM_PROFILES.get(venue_name or "")
    park_values = (
        park.features
        if park is not None and park.source_status == "available"
        else static_profile
    )
    weather_values = weather.features if weather is not None and weather.source_status == "available" else None
    if park_values and weather_values:
        return _available(
            "park_weather",
            {"park": park_values, "weather": weather_values},
            captured_at=weather.captured_at if weather is not None else captured_at,
            source=OPEN_METEO_SOURCE if weather is not None else STATIC_SOURCE,
            confidence="0.75",
            completeness="0.70",
        )
    if park_values:
        return _partial(
            "park_weather",
            "static park factors available; weather forecast missing or disabled",
            captured_at=captured_at,
            source=STATIC_SOURCE,
            confidence="0.45",
            completeness="0.35",
            values={"park": park_values, "weather_source_status": "missing"},
        )
    return _missing("park_weather", "park/weather profile unavailable", captured_at)


def _travel_module(
    row: TravelScheduleFeature | None,
    side: str,
    captured_at: datetime,
) -> dict[str, object]:
    if row is not None:
        return _module(
            f"{side}_travel_schedule",
            row.source_status,
            "cached travel feature",
            captured_at=row.captured_at,
            source=row.source,
            confidence=row.confidence or Decimal("0"),
            completeness=row.completeness or Decimal("0"),
            stale=row.stale,
            values=row.features,
        )
    return _missing(f"{side}_travel_schedule", "travel schedule cache missing", captured_at)


def _upsert_team_daily(
    session: Session,
    game: MlbGame,
    side: str,
    day: date,
    captured_at: datetime,
) -> TeamDailyFeature:
    team_code = (game.home_abbreviation if side == "home" else game.away_abbreviation) or "UNK"
    previous_games = _team_games(session, game, team_code)
    scored, allowed, sample_size = _team_run_totals(previous_games, team_code)
    record = _team_record(game, side)
    pythagorean = _pythagorean(scored, allowed)
    source_status = "partial" if sample_size or record else "missing"
    completeness = Decimal("0.45") if sample_size else Decimal("0.25") if record else Decimal("0")
    features = {
        **record,
        "sample_size": sample_size,
        "runs_per_game": _float(scored),
        "runs_allowed_per_game": _float(allowed),
        "run_differential_per_game": _float(scored - allowed) if scored is not None and allowed is not None else None,
        "pythagorean_win_pct": _float(pythagorean),
        "time_decayed_team_rating": _float(((pythagorean or Decimal("0.5000")) - Decimal("0.5000"))),
        "recent_team_strength_trend": None,
        "opponent_adjusted_proxy": None,
        "league_average_baseline": 0.5000,
        "obp": None,
        "slg": None,
        "iso": None,
        "k_rate": None,
        "bb_rate": None,
        "hr_rate": None,
        "babip": None,
        "hard_hit_pct": None,
        "barrel_pct": None,
        "average_exit_velocity": None,
        "launch_angle": None,
        "sweet_spot_proxy": None,
        "platoon_split_status": "missing",
    }
    row = session.scalar(
        select(TeamDailyFeature)
        .where(TeamDailyFeature.target_date == day)
        .where(TeamDailyFeature.team_code == team_code)
        .where(TeamDailyFeature.source == DERIVED_SOURCE)
    )
    row = row or TeamDailyFeature(target_date=day, team_code=team_code, source=DERIVED_SOURCE)
    row.captured_at = captured_at
    row.source_status = source_status
    row.confidence = Decimal("0.50") if sample_size else Decimal("0.25")
    row.completeness = completeness
    row.stale = False
    row.features = features
    row.raw_payload = {"record": record, "previous_game_count": sample_size}
    session.add(row)
    return row


def _upsert_team_recent(
    session: Session,
    game: MlbGame,
    side: str,
    day: date,
    captured_at: datetime,
    window_days: int,
) -> TeamRecentFeature:
    team_code = (game.home_abbreviation if side == "home" else game.away_abbreviation) or "UNK"
    previous_games = _team_games(session, game, team_code, days=window_days)
    scored, allowed, sample_size = _team_run_totals(previous_games, team_code)
    source_status = "partial" if sample_size else "missing"
    features = {
        "window_days": window_days,
        "runs_per_game": _float(scored),
        "runs_allowed_per_game": _float(allowed),
        "wrc_plus_proxy": None,
        "woba_proxy": None,
        "xwoba_proxy": None,
        "k_rate": None,
        "bb_rate": None,
        "iso": None,
        "hard_hit_pct": None,
        "barrel_pct": None,
        "contact_quality_proxy": None,
        "shrinkage_to_season": 0.65,
    }
    row = session.scalar(
        select(TeamRecentFeature)
        .where(TeamRecentFeature.target_date == day)
        .where(TeamRecentFeature.team_code == team_code)
        .where(TeamRecentFeature.window_days == window_days)
        .where(TeamRecentFeature.source == DERIVED_SOURCE)
    )
    row = row or TeamRecentFeature(
        target_date=day,
        team_code=team_code,
        window_days=window_days,
        source=DERIVED_SOURCE,
    )
    row.captured_at = captured_at
    row.source_status = source_status
    row.sample_size = sample_size
    row.confidence = Decimal("0.45") if sample_size else Decimal("0")
    row.completeness = Decimal("0.40") if sample_size else Decimal("0")
    row.stale = False
    row.features = features
    row.raw_payload = {"previous_game_count": sample_size}
    session.add(row)
    return row


def _upsert_pitcher(
    session: Session,
    game: MlbGame,
    side: str,
    day: date,
    captured_at: datetime,
) -> PitcherDailyFeature | None:
    team_code = (game.home_abbreviation if side == "home" else game.away_abbreviation) or "UNK"
    identity = probable_pitcher_from_payload(game.raw_payload or {}, side)
    if not identity or not identity.get("id"):
        return None
    pitcher_id = str(identity["id"])
    row = session.scalar(
        select(PitcherDailyFeature)
        .where(PitcherDailyFeature.target_date == day)
        .where(PitcherDailyFeature.team_code == team_code)
        .where(PitcherDailyFeature.pitcher_id == pitcher_id)
        .where(PitcherDailyFeature.source == MLB_STATS_SOURCE)
    )
    row = row or PitcherDailyFeature(
        target_date=day,
        team_code=team_code,
        pitcher_id=pitcher_id,
        source=MLB_STATS_SOURCE,
    )
    row.pitcher_name = str(identity.get("name") or "")
    row.captured_at = captured_at
    row.source_status = "partial"
    row.sample_size = None
    row.confidence = Decimal("0.45")
    row.completeness = Decimal("0.25")
    row.stale = False
    row.features = {
        **identity,
        "season": {
            "era": None,
            "whip": None,
            "innings_pitched": None,
            "k_rate": None,
            "bb_rate": None,
            "k_minus_bb_rate": None,
            "hr_per_9": None,
            "fip": None,
            "xfip_proxy": None,
            "xera_proxy": None,
            "xwoba_allowed": None,
            "hard_hit_allowed": None,
            "barrel_allowed": None,
            "ground_ball_proxy": None,
            "pitch_mix": None,
        },
        "recent": {
            "last_3_starts": None,
            "last_5_starts": None,
            "innings_per_start": None,
            "pitch_count": None,
            "era_proxy": None,
            "fip_proxy": None,
            "k_bb": None,
            "home_runs_allowed": None,
            "velocity_trend": None,
        },
        "workload": {
            "expected_innings_projection": None,
            "recent_pitch_count_ceiling": None,
            "days_rest": None,
            "opener_or_bulk_pitcher": None,
            "short_start_risk": None,
            "expected_bullpen_innings": None,
        },
    }
    row.raw_payload = identity
    session.add(row)
    return row


def _upsert_bullpen(
    session: Session,
    game: MlbGame,
    side: str,
    day: date,
    captured_at: datetime,
) -> BullpenDailyFeature:
    team_code = (game.home_abbreviation if side == "home" else game.away_abbreviation) or "UNK"
    row = session.scalar(
        select(BullpenDailyFeature)
        .where(BullpenDailyFeature.target_date == day)
        .where(BullpenDailyFeature.team_code == team_code)
        .where(BullpenDailyFeature.source == DERIVED_SOURCE)
    )
    row = row or BullpenDailyFeature(target_date=day, team_code=team_code, source=DERIVED_SOURCE)
    row.captured_at = captured_at
    row.source_status = "missing"
    row.confidence = Decimal("0")
    row.completeness = Decimal("0")
    row.stale = False
    row.features = {
        "era": None,
        "fip_proxy": None,
        "xfip_proxy": None,
        "whip": None,
        "k_rate": None,
        "bb_rate": None,
        "k_minus_bb_rate": None,
        "hr_per_9": None,
        "leverage_neutral_run_prevention": None,
        "expected_bullpen_innings": None,
        "recent_workload": {
            "innings_last_1_days": None,
            "innings_last_2_days": None,
            "innings_last_3_days": None,
            "appearances_last_1_days": None,
            "appearances_last_2_days": None,
            "appearances_last_3_days": None,
            "pitches_last_1_days": None,
            "pitches_last_2_days": None,
            "pitches_last_3_days": None,
            "last_7_day_performance": None,
            "last_14_day_performance": None,
            "high_leverage_availability_proxy": None,
            "expected_bullpen_fatigue_score": None,
        },
    }
    row.raw_payload = None
    session.add(row)
    return row


def _upsert_lineup(
    session: Session,
    game: MlbGame,
    side: str,
    day: date,
    captured_at: datetime,
) -> LineupSnapshot:
    team_code = (game.home_abbreviation if side == "home" else game.away_abbreviation) or "UNK"
    starters = parse_starting_lineup_from_game_payload(game.raw_payload or {}, side)
    confirmed = len(starters) == 9
    status = "available" if confirmed else "missing"
    mix = _handedness_mix(starters)
    row = session.scalar(
        select(LineupSnapshot)
        .where(LineupSnapshot.mlb_game_id == game.id)
        .where(LineupSnapshot.team_code == team_code)
        .where(LineupSnapshot.source == MLB_STATS_SOURCE)
    )
    row = row or LineupSnapshot(
        mlb_game_id=game.id,
        target_date=day,
        team_code=team_code,
        source=MLB_STATS_SOURCE,
    )
    row.captured_at = captured_at
    row.source_status = status
    row.confirmed = confirmed
    row.confidence = Decimal("0.80") if confirmed else Decimal("0")
    row.completeness = Decimal("0.85") if confirmed else Decimal("0")
    row.stale = False
    row.features = {
        "confirmed_lineup": confirmed,
        "starters": starters,
        "projected_lineup_fallback": not confirmed,
        "lineup_quality_aggregate": None,
        "top_9_wrc_plus_proxy": None,
        "top_9_woba_proxy": None,
        "top_9_xwoba_proxy": None,
        "missing_or_rested_regulars": None,
        "catcher_start_rest_impact": None,
        "handedness_mix": mix,
        "bench_downgrade": None,
        "lineup_posted_at": None,
    }
    row.raw_payload = {"starter_count": len(starters)}
    session.add(row)
    return row


def _handedness_mix(starters: list[dict[str, object]]) -> dict[str, int]:
    mix = {"L": 0, "R": 0, "S": 0, "unknown": 0}
    for starter in starters:
        side = str(starter.get("bat_side") or "").upper()
        if side in {"L", "R", "S"}:
            mix[side] += 1
        else:
            mix["unknown"] += 1
    return mix


def _upsert_injuries(
    session: Session,
    game: MlbGame,
    side: str,
    day: date,
    captured_at: datetime,
) -> InjurySnapshot:
    team_code = (game.home_abbreviation if side == "home" else game.away_abbreviation) or "UNK"
    row = session.scalar(
        select(InjurySnapshot)
        .where(InjurySnapshot.target_date == day)
        .where(InjurySnapshot.team_code == team_code)
        .where(InjurySnapshot.source == "optional_provider")
    )
    row = row or InjurySnapshot(target_date=day, team_code=team_code, source="optional_provider")
    row.captured_at = captured_at
    row.source_status = "missing"
    row.confidence = Decimal("0")
    row.completeness = Decimal("0")
    row.stale = False
    row.features = {
        "provider_configured": bool(get_settings().injury_provider_api_key),
        "players": [],
        "expected_lineup_impact": None,
        "downgrade_applied": False,
    }
    row.raw_payload = None
    session.add(row)
    return row


def _upsert_park_factor(
    session: Session,
    game: MlbGame,
    captured_at: datetime,
) -> ParkFactorSnapshot | None:
    venue_name = _venue_name(game)
    if not venue_name:
        return None
    profile = STADIUM_PROFILES.get(venue_name)
    source_status = "available" if profile else "missing"
    row = session.scalar(
        select(ParkFactorSnapshot)
        .where(ParkFactorSnapshot.venue_name == venue_name)
        .where(ParkFactorSnapshot.source == STATIC_SOURCE)
    )
    row = row or ParkFactorSnapshot(venue_name=venue_name, source=STATIC_SOURCE)
    row.captured_at = captured_at
    row.source_status = source_status
    row.confidence = Decimal("0.85") if profile else Decimal("0")
    row.completeness = Decimal("0.80") if profile else Decimal("0")
    row.stale = False
    row.features = profile or {"venue_name": venue_name, "park_factor": None}
    row.raw_payload = None
    session.add(row)
    return row


def _upsert_weather(
    session: Session,
    game: MlbGame,
    day: date,
    captured_at: datetime,
) -> WeatherSnapshot | None:
    venue_name = _venue_name(game)
    if not venue_name:
        return None
    profile = STADIUM_PROFILES.get(venue_name)
    settings = get_settings()
    source_status = "missing"
    features: dict[str, object] = {
        "temperature_2m": None,
        "relative_humidity_2m": None,
        "precipitation_probability": None,
        "precipitation": None,
        "wind_speed_10m": None,
        "wind_direction_10m": None,
        "wind_gusts_10m": None,
        "cloud_cover": None,
        "wind_orientation_status": "missing",
        "delay_postponement_risk_proxy": None,
        "roof_dome_weather_override": (profile or {}).get("roof_type") in {"dome", "retractable"}
        if profile
        else None,
    }
    raw_payload = None
    if settings.feature_sync_enable_network_sources and profile:
        try:
            raw_payload = _fetch_open_meteo(profile, game.scheduled_start)
            parsed = _parse_open_meteo(raw_payload, game.scheduled_start)
            if parsed:
                features.update(parsed)
                source_status = "available"
        except HttpJsonError as exc:
            raw_payload = {"error": exc.to_detail()}
            source_status = "missing"
    row = session.scalar(
        select(WeatherSnapshot)
        .where(WeatherSnapshot.mlb_game_id == game.id)
        .where(WeatherSnapshot.source == OPEN_METEO_SOURCE)
    )
    row = row or WeatherSnapshot(
        mlb_game_id=game.id,
        target_date=day,
        venue_name=venue_name,
        source=OPEN_METEO_SOURCE,
    )
    row.captured_at = captured_at
    row.forecast_time = ensure_aware_utc(game.scheduled_start)
    row.source_status = source_status
    row.confidence = Decimal("0.70") if source_status == "available" else Decimal("0")
    row.completeness = Decimal("0.70") if source_status == "available" else Decimal("0")
    row.stale = False
    row.features = features
    row.raw_payload = raw_payload
    session.add(row)
    return row


def _fetch_open_meteo(profile: dict[str, object], scheduled_start: datetime) -> dict[str, object]:
    settings = get_settings()
    day = ensure_aware_utc(scheduled_start).date().isoformat()
    base_url = settings.open_meteo_base_url.rstrip("/")
    forecast_url = base_url if base_url.endswith("/forecast") else f"{base_url}/forecast"
    return get_json(
        forecast_url,
        params={
            "latitude": profile["latitude"],
            "longitude": profile["longitude"],
            "hourly": [
                "temperature_2m",
                "relative_humidity_2m",
                "precipitation_probability",
                "precipitation",
                "wind_speed_10m",
                "wind_direction_10m",
                "wind_gusts_10m",
                "cloud_cover",
            ],
            "start_date": day,
            "end_date": day,
            "timezone": "UTC",
        },
    )


def _parse_open_meteo(payload: dict[str, object], scheduled_start: datetime) -> dict[str, object] | None:
    hourly = payload.get("hourly") if isinstance(payload, dict) else None
    if not isinstance(hourly, dict):
        return None
    times = hourly.get("time")
    if not isinstance(times, list) or not times:
        return None
    target_hour = ensure_aware_utc(scheduled_start).replace(minute=0, second=0, microsecond=0)
    index = 0
    for candidate_index, value in enumerate(times):
        if str(value).startswith(target_hour.strftime("%Y-%m-%dT%H")):
            index = candidate_index
            break

    def at(key: str) -> object:
        values = hourly.get(key)
        if isinstance(values, list) and index < len(values):
            return values[index]
        return None

    return {
        "temperature_2m": at("temperature_2m"),
        "relative_humidity_2m": at("relative_humidity_2m"),
        "precipitation_probability": at("precipitation_probability"),
        "precipitation": at("precipitation"),
        "wind_speed_10m": at("wind_speed_10m"),
        "wind_direction_10m": at("wind_direction_10m"),
        "wind_gusts_10m": at("wind_gusts_10m"),
        "cloud_cover": at("cloud_cover"),
    }


def _upsert_travel(
    session: Session,
    game: MlbGame,
    side: str,
    day: date,
    captured_at: datetime,
) -> TravelScheduleFeature:
    team_code = (game.home_abbreviation if side == "home" else game.away_abbreviation) or "UNK"
    previous = _team_games(session, game, team_code, days=14)
    previous_game = previous[0] if previous else None
    current_location = _team_location_for_game(game, team_code)
    previous_location = _team_location_for_game(previous_game, team_code) if previous_game else None
    distance = _distance_between_points(previous_location, current_location)
    rest_days = _rest_days(session, game, team_code)
    day_after_night = False
    if previous_game:
        previous_local = ensure_aware_utc(previous_game.scheduled_start).astimezone(get_dashboard_zone())
        current_local = ensure_aware_utc(game.scheduled_start).astimezone(get_dashboard_zone())
        day_after_night = previous_local.hour >= 18 and current_local.hour < 18
    row = session.scalar(
        select(TravelScheduleFeature)
        .where(TravelScheduleFeature.mlb_game_id == game.id)
        .where(TravelScheduleFeature.team_code == team_code)
        .where(TravelScheduleFeature.source == DERIVED_SOURCE)
    )
    row = row or TravelScheduleFeature(
        mlb_game_id=game.id,
        target_date=day,
        team_code=team_code,
        source=DERIVED_SOURCE,
    )
    row.captured_at = captured_at
    row.source_status = "partial" if rest_days is not None or distance is not None else "missing"
    row.confidence = Decimal("0.45") if row.source_status == "partial" else Decimal("0")
    row.completeness = Decimal("0.35") if row.source_status == "partial" else Decimal("0")
    row.stale = False
    row.features = {
        "rest_days": rest_days,
        "travel_distance_miles": distance,
        "time_zone_change": None,
        "road_trip_length": None,
        "home_stand_length": None,
        "getaway_day": _day_night(game) == "day",
        "day_game_after_night_game": day_after_night,
        "doubleheader": _doubleheader_flag(game),
        "prior_extra_inning_game": None,
        "prior_game_ended_late": None,
        "bullpen_fatigue_linkage": None,
    }
    row.raw_payload = {"previous_game_id": previous_game.external_game_id if previous_game else None}
    session.add(row)
    return row


def _sync_game_feature_modules(
    session: Session,
    game: MlbGame,
    day: date,
    captured_at: datetime,
    include_modules: set[str] | None,
) -> None:
    if include_modules is None or "team" in include_modules:
        for side in ("home", "away"):
            _upsert_team_daily(session, game, side, day, captured_at)
            for window in (7, 14, 30):
                _upsert_team_recent(session, game, side, day, captured_at, window)
    if include_modules is None or "pitcher" in include_modules:
        for side in ("home", "away"):
            _upsert_pitcher(session, game, side, day, captured_at)
    if include_modules is None or "bullpen" in include_modules:
        for side in ("home", "away"):
            _upsert_bullpen(session, game, side, day, captured_at)
    if include_modules is None or "lineup" in include_modules:
        for side in ("home", "away"):
            _upsert_lineup(session, game, side, day, captured_at)
    if include_modules is None or "injuries" in include_modules:
        for side in ("home", "away"):
            _upsert_injuries(session, game, side, day, captured_at)
    if include_modules is None or "weather" in include_modules:
        _upsert_park_factor(session, game, captured_at)
        _upsert_weather(session, game, day, captured_at)
    if include_modules is None or "travel" in include_modules:
        for side in ("home", "away"):
            _upsert_travel(session, game, side, day, captured_at)


def _target_games(session: Session, day: date) -> list[MlbGame]:
    local_start = datetime.combine(day, time.min, tzinfo=get_dashboard_zone())
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


def sync_mlb_features(
    session: Session,
    target_date: date | None = None,
    include_modules: set[str] | None = None,
) -> dict[str, object]:
    day = target_date or today_eastern()
    games = _target_games(session, day)
    captured_at = utc_now()
    upserted = 0
    for game in games:
        if get_settings().feature_sync_enable_network_sources and _game_phase(game) != "final":
            _hydrate_game_endpoint_if_available(game)
        _sync_game_feature_modules(session, game, day, captured_at, include_modules)
        session.flush()
        mapping = MarketMapping(
            mlb_game_id=game.id or 0,
            kalshi_market_id=0,
            mapping_status="feature_sync",
            confidence=Decimal("0.0000"),
        )
        market = KalshiMarket(
            kalshi_market_id=f"feature-sync-{game.external_game_id}",
            ticker=f"FEATURE-SYNC-{game.external_game_id}",
            title=f"{game.away_team} @ {game.home_team}",
            status="feature_sync",
        )
        features = build_feature_snapshot(game, market, mapping, session=session, now=captured_at)
        row = session.scalar(
            select(MlbFeatureSnapshot)
            .where(MlbFeatureSnapshot.mlb_game_id == game.id)
            .where(MlbFeatureSnapshot.target_date == day)
            .where(MlbFeatureSnapshot.source == FEATURE_VERSION)
        )
        row = row or MlbFeatureSnapshot(mlb_game_id=game.id, target_date=day, source=FEATURE_VERSION)
        row.captured_at = captured_at
        row.data_quality = Decimal(str(features["data_quality"])).quantize(Decimal("0.0001"))
        row.source_statuses = features.get("source_statuses")
        row.features = features
        session.add(row)
        upserted += 1
    session.commit()
    return {
        "date": day.isoformat(),
        "games_seen": len(games),
        "feature_snapshots_upserted": upserted,
        "feature_version": FEATURE_VERSION,
        "source": FEATURE_VERSION,
        "include_modules": sorted(include_modules) if include_modules else ["all"],
        "network_sources_enabled": get_settings().feature_sync_enable_network_sources,
    }


def _hydrate_game_endpoint_if_available(game: MlbGame) -> None:
    if not game.external_game_id:
        return
    try:
        payload = fetch_game_endpoint(game.external_game_id)
    except HttpJsonError:
        return
    if payload:
        merged = dict(game.raw_payload or {})
        merged.update(payload)
        game.raw_payload = merged


def sync_mlb_team_features(session: Session, target_date: date | None = None) -> dict[str, object]:
    return sync_mlb_features(session, target_date, {"team"})


def sync_mlb_pitcher_features(session: Session, target_date: date | None = None) -> dict[str, object]:
    return sync_mlb_features(session, target_date, {"pitcher"})


def sync_mlb_lineups(session: Session, target_date: date | None = None) -> dict[str, object]:
    return sync_mlb_features(session, target_date, {"lineup"})


def sync_mlb_bullpen_features(session: Session, target_date: date | None = None) -> dict[str, object]:
    return sync_mlb_features(session, target_date, {"bullpen"})


def sync_weather_features(session: Session, target_date: date | None = None) -> dict[str, object]:
    return sync_mlb_features(session, target_date, {"weather"})


def sync_travel_schedule_features(session: Session, target_date: date | None = None) -> dict[str, object]:
    return sync_mlb_features(session, target_date, {"travel"})


def feature_coverage(session: Session, target_date: date | None = None) -> dict[str, object]:
    day = target_date or today_eastern()
    rows = list(
        session.scalars(
            select(MlbFeatureSnapshot)
            .where(MlbFeatureSnapshot.target_date == day)
            .where(MlbFeatureSnapshot.source == FEATURE_VERSION)
            .order_by(MlbFeatureSnapshot.id.asc())
        )
    )
    avg_quality = None
    module_counts: dict[str, dict[str, int]] = {}
    if rows:
        avg_quality = float(sum((row.data_quality or Decimal("0")) for row in rows) / Decimal(len(rows)))
    for row in rows:
        statuses = row.source_statuses or {}
        for module_name, status in statuses.items():
            bucket = module_counts.setdefault(module_name, {})
            if isinstance(status, dict):
                flattened = "partial" if any(value != "missing" for value in status.values()) else "missing"
                bucket[flattened] = bucket.get(flattened, 0) + 1
            else:
                bucket[str(status)] = bucket.get(str(status), 0) + 1
    return {
        "date": day.isoformat(),
        "feature_version": FEATURE_VERSION,
        "snapshot_count": len(rows),
        "data_quality_avg": avg_quality,
        "module_coverage": module_counts,
        "items": [
            {
                "game_id": row.mlb_game_id,
                "source": row.source,
                "captured_at": row.captured_at.isoformat(),
                "data_quality": _float(row.data_quality),
                "source_statuses": row.source_statuses,
                "data_quality_reason": (row.features or {}).get("data_quality_reason"),
            }
            for row in rows[:200]
        ],
    }


def feature_detail(session: Session, target_date: date | None = None) -> dict[str, object]:
    day = target_date or today_eastern()
    rows = list(
        session.scalars(
            select(MlbFeatureSnapshot)
            .where(MlbFeatureSnapshot.target_date == day)
            .where(MlbFeatureSnapshot.source == FEATURE_VERSION)
            .order_by(MlbFeatureSnapshot.id.asc())
            .limit(100)
        )
    )
    return {
        "date": day.isoformat(),
        "feature_version": FEATURE_VERSION,
        "items": [
            {
                "game_id": row.mlb_game_id,
                "source": row.source,
                "captured_at": row.captured_at.isoformat(),
                "data_quality": _float(row.data_quality),
                "source_statuses": row.source_statuses,
                "features": row.features,
            }
            for row in rows
        ],
        "count": len(rows),
    }
