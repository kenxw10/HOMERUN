from __future__ import annotations

import importlib.util
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from math import atan2, cos, radians, sin, sqrt
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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
from app.services.kalshi_mlb_resolver import normalize_team_abbreviation
from app.services.mlb_stats_client import MLBStatsClient
from app.time_utils import ensure_aware_utc, get_dashboard_zone, parse_datetime, today_eastern, utc_now

FEATURE_VERSION = "mature_mlb_features_v2"
MATURE_FEATURE_VERSION = FEATURE_VERSION
FEATURE_SYNC_AUDIT_SOURCE = f"{FEATURE_VERSION}_sync_audit"
LEAGUE_AVG_FULL_GAME_RUNS = Decimal("4.35")
LEAGUE_AVG_FIRST_FIVE_RUNS = Decimal("2.15")
EARTH_RADIUS_MILES = Decimal("3958.8")
STATIC_SOURCE = "static_mlb_reference_v1"
MLB_STATS_SOURCE = "mlb_stats_api"
OPEN_METEO_SOURCE = "open_meteo"
DERIVED_SOURCE = "derived_homerun_v2"
NETWORK_SOURCE_MODULES = {"team", "pitcher", "bullpen", "lineup", "weather"}
ALL_SYNC_MODULES = NETWORK_SOURCE_MODULES | {"injuries", "travel"}
RAW_TABLES_BY_MODULE = {
    "team": ("team_daily_features", "team_recent_features"),
    "pitcher": ("pitcher_daily_features",),
    "bullpen": ("bullpen_daily_features",),
    "lineup": ("lineup_snapshots",),
    "injuries": ("injury_snapshots",),
    "weather": ("weather_snapshots", "park_factor_snapshots"),
    "travel": ("travel_schedule_features",),
}

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

def _park(
    latitude: float,
    longitude: float,
    altitude_ft: int,
    roof_type: str,
    park_factor: float,
    run_factor: float,
    hr_factor: float,
    orientation_degrees: int,
) -> dict[str, object]:
    return {
        "latitude": latitude,
        "longitude": longitude,
        "altitude_ft": altitude_ft,
        "roof_type": roof_type,
        "park_factor": park_factor,
        "run_factor": run_factor,
        "hr_factor": hr_factor,
        "orientation_degrees": orientation_degrees,
    }


STADIUM_PROFILES: dict[str, dict[str, object]] = {
    "American Family Field": _park(43.0280, -87.9712, 602, "retractable", 1.00, 1.00, 1.05, 70),
    "Angel Stadium": _park(33.8003, -117.8827, 160, "open", 0.99, 0.99, 1.00, 65),
    "Busch Stadium": _park(38.6226, -90.1928, 466, "open", 0.98, 0.98, 0.94, 62),
    "Chase Field": _park(33.4455, -112.0667, 1086, "retractable", 1.01, 1.01, 1.02, 60),
    "Citi Field": _park(40.7571, -73.8458, 13, "open", 0.98, 0.98, 0.96, 67),
    "Citizens Bank Park": _park(39.9061, -75.1665, 20, "open", 1.03, 1.02, 1.13, 70),
    "Comerica Park": _park(42.3390, -83.0485, 600, "open", 0.99, 1.00, 0.92, 150),
    "Coors Field": _park(39.7561, -104.9942, 5200, "open", 1.19, 1.22, 1.15, 35),
    "Daikin Park": _park(29.7573, -95.3555, 50, "retractable", 1.01, 1.01, 1.04, 80),
    "Dodger Stadium": _park(34.0739, -118.2400, 522, "open", 1.00, 1.00, 1.02, 36),
    "Fenway Park": _park(42.3467, -71.0972, 20, "open", 1.05, 1.06, 0.97, 45),
    "George M. Steinbrenner Field": _park(27.9803, -82.5067, 45, "open", 1.00, 1.00, 1.00, 55),
    "Globe Life Field": _park(32.7473, -97.0842, 600, "retractable", 1.01, 1.01, 1.02, 30),
    "Great American Ball Park": _park(39.0979, -84.5082, 482, "open", 1.08, 1.07, 1.18, 130),
    "Guaranteed Rate Field": _park(41.8299, -87.6338, 594, "open", 1.00, 1.00, 1.03, 127),
    "Kauffman Stadium": _park(39.0517, -94.4803, 750, "open", 0.99, 1.00, 0.93, 65),
    "Las Vegas Ballpark": _park(36.1596, -115.3320, 2960, "open", 1.02, 1.02, 1.06, 62),
    "loanDepot park": _park(25.7781, -80.2197, 10, "retractable", 0.96, 0.96, 0.88, 73),
    "Nationals Park": _park(38.8730, -77.0074, 25, "open", 1.00, 1.00, 1.02, 60),
    "Oracle Park": _park(37.7786, -122.3893, 63, "open", 0.93, 0.94, 0.78, 85),
    "Oriole Park at Camden Yards": _park(39.2839, -76.6217, 33, "open", 0.99, 0.99, 0.93, 60),
    "Petco Park": _park(32.7073, -117.1573, 62, "open", 0.94, 0.95, 0.88, 80),
    "PNC Park": _park(40.4469, -80.0057, 730, "open", 0.98, 0.99, 0.95, 115),
    "Progressive Field": _park(41.4962, -81.6852, 653, "open", 0.99, 0.99, 0.98, 60),
    "Rate Field": _park(41.8299, -87.6338, 594, "open", 1.00, 1.00, 1.03, 127),
    "Rogers Centre": _park(43.6414, -79.3894, 250, "retractable", 1.01, 1.01, 1.05, 45),
    "Sutter Health Park": _park(38.5804, -121.5139, 23, "open", 1.03, 1.03, 1.08, 55),
    "Target Field": _park(44.9817, -93.2776, 840, "open", 0.99, 0.99, 1.00, 100),
    "T-Mobile Park": _park(47.5914, -122.3325, 10, "retractable", 0.92, 0.91, 0.90, 130),
    "Truist Park": _park(33.8908, -84.4678, 1050, "open", 1.01, 1.01, 1.04, 145),
    "Wrigley Field": _park(41.9484, -87.6553, 600, "open", 1.02, 1.02, 1.05, 50),
    "Yankee Stadium": _park(40.8296, -73.9262, 55, "open", 1.02, 1.01, 1.10, 75),
}

TEAM_HOME_VENUES: dict[str, str] = {
    "ARI": "Chase Field",
    "ATH": "Sutter Health Park",
    "ATL": "Truist Park",
    "BAL": "Oriole Park at Camden Yards",
    "BOS": "Fenway Park",
    "CHC": "Wrigley Field",
    "CIN": "Great American Ball Park",
    "CLE": "Progressive Field",
    "COL": "Coors Field",
    "CWS": "Rate Field",
    "DET": "Comerica Park",
    "HOU": "Daikin Park",
    "KC": "Kauffman Stadium",
    "LAA": "Angel Stadium",
    "LAD": "Dodger Stadium",
    "MIA": "loanDepot park",
    "MIL": "American Family Field",
    "MIN": "Target Field",
    "NYM": "Citi Field",
    "NYY": "Yankee Stadium",
    "OAK": "Sutter Health Park",
    "PHI": "Citizens Bank Park",
    "PIT": "PNC Park",
    "SD": "Petco Park",
    "SEA": "T-Mobile Park",
    "SF": "Oracle Park",
    "STL": "Busch Stadium",
    "TB": "George M. Steinbrenner Field",
    "TEX": "Globe Life Field",
    "TOR": "Rogers Centre",
    "WSH": "Nationals Park",
}

TEAM_HOME_COORDINATES: dict[str, tuple[float, float]] = {
    team_code: (
        float(STADIUM_PROFILES[venue_name]["latitude"]),
        float(STADIUM_PROFILES[venue_name]["longitude"]),
    )
    for team_code, venue_name in TEAM_HOME_VENUES.items()
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
    if team_code in {game.home_abbreviation, game.away_abbreviation}:
        return TEAM_HOME_COORDINATES.get(game.home_abbreviation or "")
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
                "full_name": person.get("fullName") or player.get("fullName"),
                "name": person.get("fullName") or player.get("fullName"),
                "batting_order": batting_order,
                "batting_order_slot": batting_order // 100,
                "handedness": batting_side.get("code") or batting_side.get("description"),
                "bat_side": batting_side.get("code") or batting_side.get("description"),
                "position": primary_position.get("abbreviation") or primary_position.get("code"),
                "is_starter": True,
                "is_catcher": (primary_position.get("abbreviation") or primary_position.get("code")) == "C",
            }
        )
    return sorted(starters, key=lambda item: int(item["batting_order"]))


def probable_pitcher_from_payload(payload: dict[str, object], side: str) -> dict[str, object] | None:
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
                "pitcher_name": person.get("fullName") or player.get("fullName"),
                "handedness": pitch_hand.get("code") or pitch_hand.get("description"),
                "note": player.get("note"),
                "source_path": f"game.liveData.boxscore.teams.{side}.pitchers[0]",
            }

    teams = payload.get("teams") if isinstance(payload, dict) else None
    team = teams.get(side) if isinstance(teams, dict) else None
    pitcher = team.get("probablePitcher") if isinstance(team, dict) else None
    if isinstance(pitcher, dict) and pitcher.get("id"):
        return {
            "id": str(pitcher.get("id")),
            "name": pitcher.get("fullName"),
            "pitcher_name": pitcher.get("fullName"),
            "handedness": None,
            "note": pitcher.get("note"),
            "source_path": f"schedule.teams.{side}.probablePitcher",
        }
    return None


def _game_endpoint_url(game_pk: str) -> str:
    base = get_settings().mlb_stats_base_url.rstrip("/")
    return f"{base}/game/{game_pk}/feed/live"


def fetch_game_endpoint(game_pk: str) -> dict[str, object]:
    return get_json(_game_endpoint_url(game_pk), params={})


def pybaseball_available() -> bool:
    return importlib.util.find_spec("pybaseball") is not None


def advanced_public_stats_status() -> str:
    if pybaseball_available():
        return "not_ingested_pybaseball_adapter_not_implemented"
    return "unavailable_pybaseball_not_installed"


def _merge_game_payload(game: MlbGame, payload: dict[str, object]) -> None:
    merged = dict(game.raw_payload or {})
    merged.update(payload)
    game.raw_payload = merged


def _upsert_game_from_schedule_payload(session: Session, payload: dict[str, object]) -> MlbGame | None:
    game_pk = str(payload.get("gamePk") or "")
    scheduled_start = parse_datetime(payload.get("gameDate"))
    if not game_pk or scheduled_start is None:
        return None
    teams = payload.get("teams") if isinstance(payload.get("teams"), dict) else {}
    home = teams.get("home") if isinstance(teams, dict) and isinstance(teams.get("home"), dict) else {}
    away = teams.get("away") if isinstance(teams, dict) and isinstance(teams.get("away"), dict) else {}
    home_team_payload = home.get("team") if isinstance(home.get("team"), dict) else {}
    away_team_payload = away.get("team") if isinstance(away.get("team"), dict) else {}
    status_payload = payload.get("status") if isinstance(payload.get("status"), dict) else {}
    row = session.scalar(select(MlbGame).where(MlbGame.external_game_id == game_pk))
    row = row or MlbGame(external_game_id=game_pk)
    row.home_team = str(home_team_payload.get("name") or "UNKNOWN HOME")
    row.away_team = str(away_team_payload.get("name") or "UNKNOWN AWAY")
    row.home_abbreviation = normalize_team_abbreviation(
        row.home_team,
        home_team_payload.get("abbreviation"),
    )
    row.away_abbreviation = normalize_team_abbreviation(
        row.away_team,
        away_team_payload.get("abbreviation"),
    )
    row.scheduled_start = scheduled_start
    row.status = (
        status_payload.get("detailedState")
        or status_payload.get("abstractGameState")
        or row.status
        or "scheduled"
    )
    row.home_score = home.get("score")
    row.away_score = away.get("score")
    _merge_game_payload(row, payload)
    session.add(row)
    return row


def _schedule_games_from_payload(payload: dict[str, object]) -> list[dict[str, object]]:
    games: list[dict[str, object]] = []
    dates = payload.get("dates") if isinstance(payload, dict) else None
    if not isinstance(dates, list):
        return games
    for schedule_date in dates:
        day_games = schedule_date.get("games") if isinstance(schedule_date, dict) else None
        if isinstance(day_games, list):
            games.extend(game for game in day_games if isinstance(game, dict))
    return games


def _source_error(
    *,
    source: str,
    table: str,
    exc: BaseException,
    game_pk: object | None = None,
) -> dict[str, object]:
    error: dict[str, object] = {
        "source": source,
        "table": table,
        "error_type": exc.__class__.__name__,
        "message": str(getattr(exc, "orig", exc)),
    }
    if game_pk is not None:
        error["game_pk"] = str(game_pk)
    if isinstance(exc, HttpJsonError):
        error["detail"] = exc.to_detail()
    return error


def _hydrate_schedule_window(
    session: Session,
    day: date,
    *,
    client: MLBStatsClient,
    errors: list[dict[str, object]],
) -> dict[str, object]:
    stats: dict[str, object] = {
        "rows_seen": 0,
        "rows_upserted": 0,
        "duplicate_count": 0,
        "error_count": 0,
        "validation_status": "ok",
        "warnings": [],
        "errors": [],
    }
    deduped: dict[str, tuple[str, dict[str, object]]] = {}

    def add_error(error: dict[str, object]) -> None:
        stats_errors = stats.setdefault("errors", [])
        if isinstance(stats_errors, list):
            stats_errors.append(error)
        errors.append(error)
        stats["error_count"] = int(stats.get("error_count", 0)) + 1

    def add_warning(message: str) -> None:
        warnings = stats.setdefault("warnings", [])
        if isinstance(warnings, list) and message not in warnings:
            warnings.append(message)

    def collect(table: str, payload: dict[str, object]) -> None:
        for game_payload in _schedule_games_from_payload(payload):
            game_pk = str(game_payload.get("gamePk") or "")
            if not game_pk:
                add_error(
                    {
                        "source": MLB_STATS_SOURCE,
                        "table": table,
                        "error_type": "ValueError",
                        "message": "MLB schedule game missing gamePk.",
                    }
                )
                continue
            stats["rows_seen"] = int(stats.get("rows_seen", 0)) + 1
            if game_pk in deduped:
                stats["duplicate_count"] = int(stats.get("duplicate_count", 0)) + 1
                add_warning(f"Duplicate MLB schedule gamePk {game_pk} ignored during hydration.")
                continue
            deduped[game_pk] = (table, game_payload)

    try:
        collect("mlb_games", client.get_schedule(day))
    except (HttpJsonError, ValueError, KeyError, TypeError) as exc:
        add_error(_source_error(source=MLB_STATS_SOURCE, table="mlb_games", exc=exc))
    except Exception as exc:  # defensive: source failures should degrade the sync, not 500
        add_error(_source_error(source=MLB_STATS_SOURCE, table="mlb_games", exc=exc))

    try:
        collect(
            "mlb_games_history",
            client.get_schedule(
                start_date=day - timedelta(days=45),
                end_date=day - timedelta(days=1),
                hydrate="team,venue,linescore",
            ),
        )
    except (HttpJsonError, ValueError, KeyError, TypeError) as exc:
        add_error(_source_error(source=MLB_STATS_SOURCE, table="mlb_games_history", exc=exc))
    except Exception as exc:  # defensive: source failures should degrade the sync, not 500
        add_error(_source_error(source=MLB_STATS_SOURCE, table="mlb_games_history", exc=exc))

    for game_pk, (table, game_payload) in deduped.items():
        try:
            with session.begin_nested():
                if _upsert_game_from_schedule_payload(session, game_payload) is not None:
                    session.flush()
                    stats["rows_upserted"] = int(stats.get("rows_upserted", 0)) + 1
        except (IntegrityError, ValueError, KeyError, TypeError) as exc:
            add_error(_source_error(source=MLB_STATS_SOURCE, table=table, game_pk=game_pk, exc=exc))
        except Exception as exc:  # defensive: one bad game row should not abort feature sync
            add_error(_source_error(source=MLB_STATS_SOURCE, table=table, game_pk=game_pk, exc=exc))

    if int(stats.get("error_count", 0)) > 0:
        stats["validation_status"] = "degraded_with_errors"
    elif int(stats.get("duplicate_count", 0)) > 0:
        stats["validation_status"] = "ok_with_duplicates"
    return stats


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
    market_context_values = {
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
    }
    if mapping.mapping_status == "feature_sync":
        market_context = _module(
            "market_context",
            "missing",
            "feature-only sync has no real Kalshi market context",
            captured_at=captured_at,
            source="feature_sync_placeholder",
            values=market_context_values,
        )
    else:
        market_context = _available(
            "market_context",
            market_context_values,
            captured_at=captured_at,
            source="kalshi_public_market_data",
            confidence="0.90",
            completeness="0.85",
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
        "market_context": market_context,
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
    runs_index = (scored / LEAGUE_AVG_FULL_GAME_RUNS).quantize(Decimal("0.0001")) if scored else None
    wrc_plus_proxy = int((runs_index or Decimal("1.0000")) * Decimal("100")) if sample_size >= 10 else None
    woba_proxy = (Decimal("0.320") * (runs_index or Decimal("1.0000"))).quantize(Decimal("0.0001")) if sample_size >= 10 else None
    iso_proxy = (Decimal("0.160") * (runs_index or Decimal("1.0000"))).quantize(Decimal("0.0001")) if sample_size >= 10 else None
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
        "iso": _float(iso_proxy),
        "k_rate": None,
        "bb_rate": None,
        "hr_rate": None,
        "babip": None,
        "wrc_plus_proxy": wrc_plus_proxy,
        "woba_proxy": _float(woba_proxy),
        "xwoba_proxy": None,
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
    runs_index = (scored / LEAGUE_AVG_FULL_GAME_RUNS).quantize(Decimal("0.0001")) if scored else None
    woba_proxy = (Decimal("0.320") * (runs_index or Decimal("1.0000"))).quantize(Decimal("0.0001")) if sample_size >= 3 else None
    iso_proxy = (Decimal("0.160") * (runs_index or Decimal("1.0000"))).quantize(Decimal("0.0001")) if sample_size >= 3 else None
    source_status = "partial" if sample_size else "missing"
    features = {
        "window_days": window_days,
        "runs_per_game": _float(scored),
        "runs_allowed_per_game": _float(allowed),
        "wrc_plus_proxy": None,
        "woba_proxy": _float(woba_proxy),
        "xwoba_proxy": None,
        "k_rate": None,
        "bb_rate": None,
        "iso": _float(iso_proxy),
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
    recent_games = _team_games(session, game, team_code, days=14)
    one_day_games = _team_games(session, game, team_code, days=1)
    two_day_games = _team_games(session, game, team_code, days=2)
    three_day_games = _team_games(session, game, team_code, days=3)
    _scored, allowed, sample_size = _team_run_totals(recent_games, team_code)
    workload_3 = len(three_day_games)
    fatigue_score = min(1.0, (len(one_day_games) * 0.45) + (len(two_day_games) * 0.20) + (workload_3 * 0.10))
    source_status = "partial" if sample_size else "missing"
    row = session.scalar(
        select(BullpenDailyFeature)
        .where(BullpenDailyFeature.target_date == day)
        .where(BullpenDailyFeature.team_code == team_code)
        .where(BullpenDailyFeature.source == DERIVED_SOURCE)
    )
    row = row or BullpenDailyFeature(target_date=day, team_code=team_code, source=DERIVED_SOURCE)
    row.captured_at = captured_at
    row.source_status = source_status
    row.confidence = Decimal("0.40") if sample_size else Decimal("0")
    row.completeness = Decimal("0.35") if sample_size else Decimal("0")
    row.stale = False
    row.features = {
        "sample_size": sample_size,
        "era": _float(allowed),
        "fip_proxy": None,
        "xfip_proxy": None,
        "whip": None,
        "k_rate": None,
        "bb_rate": None,
        "k_minus_bb_rate": None,
        "hr_per_9": None,
        "leverage_neutral_run_prevention": _float(allowed),
        "expected_bullpen_innings": 3.5 if sample_size else None,
        "recent_workload": {
            "innings_last_1_days": len(one_day_games) * 3,
            "innings_last_2_days": len(two_day_games) * 3,
            "innings_last_3_days": workload_3 * 3,
            "appearances_last_1_days": len(one_day_games),
            "appearances_last_2_days": len(two_day_games),
            "appearances_last_3_days": workload_3,
            "pitches_last_1_days": None,
            "pitches_last_2_days": None,
            "pitches_last_3_days": None,
            "last_7_day_performance": _float(_team_run_totals(_team_games(session, game, team_code, days=7), team_code)[1]),
            "last_14_day_performance": _float(allowed),
            "high_leverage_availability_proxy": None,
            "expected_bullpen_fatigue_score": fatigue_score if sample_size else None,
        },
    }
    row.raw_payload = {"recent_game_count": sample_size, "source": "team_game_log_proxy"}
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
    if not confirmed and not starters and row.confirmed and row.source_status == "available":
        return row
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
        "missing_reason": None if confirmed else "LINEUP_NOT_POSTED_YET",
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
        "rain": None,
        "wind_speed_10m": None,
        "wind_direction_10m": None,
        "wind_gusts_10m": None,
        "cloud_cover": None,
        "wind_orientation_status": "missing",
        "delay_postponement_risk_proxy": None,
        "missing_reason": "WEATHER_UNAVAILABLE" if profile else "STADIUM_COORDINATES_MISSING",
        "roof_or_dome": (profile or {}).get("roof_type") in {"dome", "retractable"} if profile else None,
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
                features["missing_reason"] = None
                source_status = "available"
        except HttpJsonError as exc:
            raw_payload = {
                "error": _source_error(
                    source=OPEN_METEO_SOURCE,
                    table="weather_snapshots",
                    game_pk=game.external_game_id,
                    exc=exc,
                )
            }
            source_status = "missing"
        except (ValueError, KeyError, TypeError) as exc:
            raw_payload = {
                "error": _source_error(
                    source=OPEN_METEO_SOURCE,
                    table="weather_snapshots",
                    game_pk=game.external_game_id,
                    exc=exc,
                )
            }
            source_status = "missing"
        except Exception as exc:  # defensive: weather source failures should degrade, not 500
            raw_payload = {
                "error": _source_error(
                    source=OPEN_METEO_SOURCE,
                    table="weather_snapshots",
                    game_pk=game.external_game_id,
                    exc=exc,
                )
            }
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
    if (
        not settings.feature_sync_enable_network_sources
        and row.source_status == "available"
        and row.venue_name == venue_name
    ):
        return row
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
    day = ensure_aware_utc(scheduled_start).astimezone(get_dashboard_zone()).date().isoformat()
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
                "rain",
                "wind_speed_10m",
                "wind_direction_10m",
                "wind_gusts_10m",
                "cloud_cover",
            ],
            "start_date": day,
            "end_date": day,
            "timezone": "America/New_York",
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
        },
    )


def _parse_open_meteo(payload: dict[str, object], scheduled_start: datetime) -> dict[str, object] | None:
    hourly = payload.get("hourly") if isinstance(payload, dict) else None
    if not isinstance(hourly, dict):
        return None
    times = hourly.get("time")
    if not isinstance(times, list) or not times:
        return None
    target_hour = ensure_aware_utc(scheduled_start).astimezone(get_dashboard_zone()).replace(
        minute=0,
        second=0,
        microsecond=0,
    )
    index = None
    nearest_delta: float | None = None
    for candidate_index, value in enumerate(times):
        try:
            candidate_time = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            if str(value).startswith(target_hour.strftime("%Y-%m-%dT%H")):
                index = candidate_index
                break
            continue
        if candidate_time.tzinfo is None:
            candidate_time = candidate_time.replace(tzinfo=get_dashboard_zone())
        delta = abs((ensure_aware_utc(candidate_time) - ensure_aware_utc(target_hour)).total_seconds())
        if nearest_delta is None or delta < nearest_delta:
            index = candidate_index
            nearest_delta = delta
    if index is None:
        return None

    def at(key: str) -> object:
        values = hourly.get(key)
        if isinstance(values, list) and index < len(values):
            return values[index]
        return None

    temperature = at("temperature_2m")
    humidity = at("relative_humidity_2m")
    wind_speed = at("wind_speed_10m")
    return {
        "forecast_time": str(times[index]),
        "temperature": temperature,
        "temperature_2m": temperature,
        "humidity": humidity,
        "relative_humidity_2m": humidity,
        "precipitation_probability": at("precipitation_probability"),
        "precipitation": at("precipitation"),
        "rain": at("rain"),
        "wind_speed": wind_speed,
        "wind_speed_10m": wind_speed,
        "wind_direction": at("wind_direction_10m"),
        "wind_direction_10m": at("wind_direction_10m"),
        "wind_gusts": at("wind_gusts_10m"),
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


def _new_sync_stats(day: date, include_modules: set[str] | None) -> dict[str, object]:
    modules = sorted(include_modules) if include_modules else ["all"]
    return {
        "target_date": day.isoformat(),
        "network_sources_enabled": get_settings().feature_sync_enable_network_sources,
        "games_seen": 0,
        "rows_attempted": 0,
        "rows_inserted": 0,
        "rows_updated": 0,
        "available_count": 0,
        "partial_count": 0,
        "missing_count": 0,
        "error_count": 0,
        "validation_status": "ok",
        "refresh_schedule": None,
        "hydration_rows_seen": 0,
        "hydration_rows_upserted": 0,
        "hydration_duplicate_count": 0,
        "hydration_error_count": 0,
        "hydration_validation_status": "not_run",
        "hydration_skipped_reason": None,
        "warnings": [],
        "errors": [],
        "tables_written": [],
        "source_summary": {},
        "feature_snapshots_upserted": 0,
        "feature_version": FEATURE_VERSION,
        "source": FEATURE_VERSION,
        "include_modules": modules,
    }


def _requested_modules(include_modules: set[str] | None) -> set[str]:
    return set(include_modules) if include_modules else set(ALL_SYNC_MODULES)


def _append_warning(stats: dict[str, object], warning: str) -> None:
    warnings = stats.setdefault("warnings", [])
    if isinstance(warnings, list) and warning not in warnings:
        warnings.append(warning)


def _append_error(stats: dict[str, object], error: dict[str, object]) -> None:
    errors = stats.setdefault("errors", [])
    if isinstance(errors, list):
        errors.append(error)
    stats["error_count"] = int(stats.get("error_count", 0)) + 1


def _merge_hydration_stats(stats: dict[str, object], hydration: object) -> None:
    if isinstance(hydration, int):
        stats["hydration_rows_upserted"] = int(stats.get("hydration_rows_upserted", 0)) + hydration
        stats["hydration_validation_status"] = "ok"
        return
    if not isinstance(hydration, dict):
        return
    stats["hydration_rows_seen"] = int(stats.get("hydration_rows_seen", 0)) + int(hydration.get("rows_seen", 0) or 0)
    stats["hydration_rows_upserted"] = int(stats.get("hydration_rows_upserted", 0)) + int(hydration.get("rows_upserted", 0) or 0)
    stats["hydration_duplicate_count"] = int(stats.get("hydration_duplicate_count", 0)) + int(hydration.get("duplicate_count", 0) or 0)
    stats["hydration_error_count"] = int(stats.get("hydration_error_count", 0)) + int(hydration.get("error_count", 0) or 0)
    stats["hydration_validation_status"] = str(hydration.get("validation_status") or "ok")
    for warning in hydration.get("warnings", []) if isinstance(hydration.get("warnings"), list) else []:
        _append_warning(stats, str(warning))


def _record_feature_row(stats: dict[str, object], table: str, row: object | None) -> None:
    stats["rows_attempted"] = int(stats.get("rows_attempted", 0)) + 1
    source_summary = stats.setdefault("source_summary", {})
    if not isinstance(source_summary, dict):
        source_summary = {}
        stats["source_summary"] = source_summary
    table_summary = source_summary.setdefault(
        table,
        {"attempted": 0, "inserted": 0, "updated": 0, "available": 0, "partial": 0, "missing": 0},
    )
    if isinstance(table_summary, dict):
        table_summary["attempted"] = int(table_summary.get("attempted", 0)) + 1

    if row is None:
        stats["missing_count"] = int(stats.get("missing_count", 0)) + 1
        if isinstance(table_summary, dict):
            table_summary["missing"] = int(table_summary.get("missing", 0)) + 1
        return

    is_insert = getattr(row, "id", None) is None
    if is_insert:
        stats["rows_inserted"] = int(stats.get("rows_inserted", 0)) + 1
        if isinstance(table_summary, dict):
            table_summary["inserted"] = int(table_summary.get("inserted", 0)) + 1
    else:
        stats["rows_updated"] = int(stats.get("rows_updated", 0)) + 1
        if isinstance(table_summary, dict):
            table_summary["updated"] = int(table_summary.get("updated", 0)) + 1

    tables_written = stats.setdefault("tables_written", [])
    if isinstance(tables_written, list) and table not in tables_written:
        tables_written.append(table)

    status = str(getattr(row, "source_status", "missing") or "missing")
    bucket_key = f"{status}_count"
    if bucket_key in stats:
        stats[bucket_key] = int(stats.get(bucket_key, 0)) + 1
    if isinstance(table_summary, dict):
        table_summary[status] = int(table_summary.get(status, 0)) + 1


def _upsert_feature_sync_audit(
    session: Session,
    day: date,
    captured_at: datetime,
    sync_status: dict[str, object],
) -> MlbFeatureSnapshot:
    row = session.scalar(
        select(MlbFeatureSnapshot)
        .where(MlbFeatureSnapshot.mlb_game_id.is_(None))
        .where(MlbFeatureSnapshot.target_date == day)
        .where(MlbFeatureSnapshot.source == FEATURE_SYNC_AUDIT_SOURCE)
        .order_by(MlbFeatureSnapshot.id.desc())
        .limit(1)
    )
    row = row or MlbFeatureSnapshot(
        mlb_game_id=None,
        target_date=day,
        source=FEATURE_SYNC_AUDIT_SOURCE,
    )
    row.captured_at = captured_at
    row.data_quality = None
    row.source_statuses = {"sync": sync_status.get("validation_status")}
    row.features = {"sync_status": sync_status}
    session.add(row)
    return row


def _sync_game_feature_modules(
    session: Session,
    game: MlbGame,
    day: date,
    captured_at: datetime,
    include_modules: set[str] | None,
    stats: dict[str, object],
) -> None:
    if include_modules is None or "team" in include_modules:
        for side in ("home", "away"):
            _record_feature_row(
                stats,
                "team_daily_features",
                _upsert_team_daily(session, game, side, day, captured_at),
            )
            for window in (7, 14, 30):
                _record_feature_row(
                    stats,
                    "team_recent_features",
                    _upsert_team_recent(session, game, side, day, captured_at, window),
                )
    if include_modules is None or "pitcher" in include_modules:
        for side in ("home", "away"):
            _record_feature_row(
                stats,
                "pitcher_daily_features",
                _upsert_pitcher(session, game, side, day, captured_at),
            )
    if include_modules is None or "bullpen" in include_modules:
        for side in ("home", "away"):
            _record_feature_row(
                stats,
                "bullpen_daily_features",
                _upsert_bullpen(session, game, side, day, captured_at),
            )
    if include_modules is None or "lineup" in include_modules:
        for side in ("home", "away"):
            _record_feature_row(
                stats,
                "lineup_snapshots",
                _upsert_lineup(session, game, side, day, captured_at),
            )
    if include_modules is None or "injuries" in include_modules:
        for side in ("home", "away"):
            _record_feature_row(
                stats,
                "injury_snapshots",
                _upsert_injuries(session, game, side, day, captured_at),
            )
    if include_modules is None or "weather" in include_modules:
        _record_feature_row(stats, "park_factor_snapshots", _upsert_park_factor(session, game, captured_at))
        _record_feature_row(stats, "weather_snapshots", _upsert_weather(session, game, day, captured_at))
    if include_modules is None or "travel" in include_modules:
        for side in ("home", "away"):
            _record_feature_row(
                stats,
                "travel_schedule_features",
                _upsert_travel(session, game, side, day, captured_at),
            )


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
    refresh_schedule: bool | None = None,
) -> dict[str, object]:
    day = target_date or today_eastern()
    requested_modules = _requested_modules(include_modules)
    stats = _new_sync_stats(day, include_modules)
    settings = get_settings()
    if not settings.feature_sync_enable_network_sources and requested_modules & NETWORK_SOURCE_MODULES:
        games = _target_games(session, day)
        stats["games_seen"] = len(games)
        stats["validation_status"] = "skipped_network_disabled"
        stats["network_sources_enabled"] = False
        _append_warning(
            stats,
            "FEATURE_SYNC_ENABLE_NETWORK_SOURCES=false; public source ingestion was skipped.",
        )
        return stats

    errors: list[dict[str, object]] = []
    games_before_hydration = _target_games(session, day)
    if refresh_schedule is None:
        refresh_schedule = include_modules is None or len(games_before_hydration) == 0
    stats["refresh_schedule"] = refresh_schedule
    if settings.feature_sync_enable_network_sources and refresh_schedule:
        client = MLBStatsClient()
        hydration = _hydrate_schedule_window(session, day, client=client, errors=errors)
        _merge_hydration_stats(stats, hydration)
        if int(stats.get("hydration_rows_upserted", 0)) == 0:
            _append_warning(stats, "MLB schedule hydration returned no games.")
    elif settings.feature_sync_enable_network_sources:
        stats["hydration_skipped_reason"] = (
            "target_date_games_exist" if games_before_hydration else "refresh_schedule_false"
        )
    games = _target_games(session, day)
    captured_at = utc_now()
    upserted = 0
    snapshot_rows: list[MlbFeatureSnapshot] = []
    stats["games_seen"] = len(games)
    for error in errors:
        _append_error(stats, error)
    if not pybaseball_available() and requested_modules & {"team", "pitcher", "bullpen"}:
        _append_warning(
            stats,
            "pybaseball is unavailable; advanced public offense/pitching stats are degraded to MLB schedule-derived partial proxies.",
        )
    for game in games:
        if settings.feature_sync_enable_network_sources:
            error = _hydrate_game_endpoint_if_available(game)
            if error:
                _append_error(stats, error)
        _sync_game_feature_modules(session, game, day, captured_at, include_modules, stats)
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
        snapshot_rows.append(row)
        upserted += 1
    stats["feature_snapshots_upserted"] = upserted
    if int(stats.get("error_count", 0)) > 0:
        stats["validation_status"] = "degraded_with_errors"
    elif int(stats.get("games_seen", 0)) == 0:
        stats["validation_status"] = "degraded_no_games"
    elif int(stats.get("rows_inserted", 0)) + int(stats.get("rows_updated", 0)) == 0:
        stats["validation_status"] = "degraded_no_rows_written"
    elif int(stats.get("available_count", 0)) == 0 and requested_modules & NETWORK_SOURCE_MODULES:
        stats["validation_status"] = "degraded_no_available_public_rows"
    else:
        stats["validation_status"] = "ok"
    if isinstance(stats.get("tables_written"), list):
        stats["tables_written"] = sorted(stats["tables_written"])
    if int(stats.get("rows_inserted", 0)) + int(stats.get("rows_updated", 0)) == 0:
        _append_warning(stats, "No raw feature rows were inserted or updated; inspect validation_status and errors.")
    sync_status = {
        "target_date": stats["target_date"],
        "attempted_at": captured_at.isoformat(),
        "validation_status": stats["validation_status"],
        "error_count": stats["error_count"],
        "errors": stats["errors"],
        "warnings": stats["warnings"],
        "hydration_validation_status": stats["hydration_validation_status"],
        "hydration_error_count": stats["hydration_error_count"],
        "hydration_duplicate_count": stats["hydration_duplicate_count"],
    }
    for row in snapshot_rows:
        row.features = {**(row.features or {}), "sync_status": sync_status}
    _upsert_feature_sync_audit(session, day, captured_at, sync_status)
    session.commit()
    return stats


def _hydrate_game_endpoint_if_available(game: MlbGame) -> dict[str, object] | None:
    if not game.external_game_id or not str(game.external_game_id).isdigit():
        return None
    try:
        payload = MLBStatsClient().get_game_feed(game.external_game_id)
    except HttpJsonError as exc:
        return _source_error(source=MLB_STATS_SOURCE, table="mlb_games_feed", game_pk=game.external_game_id, exc=exc)
    except (ValueError, KeyError, TypeError) as exc:
        return _source_error(source=MLB_STATS_SOURCE, table="mlb_games_feed", game_pk=game.external_game_id, exc=exc)
    except Exception as exc:  # defensive: source failures should degrade the sync, not 500
        return _source_error(source=MLB_STATS_SOURCE, table="mlb_games_feed", game_pk=game.external_game_id, exc=exc)
    if payload:
        _merge_game_payload(game, payload)
    return None


def sync_mlb_team_features(
    session: Session,
    target_date: date | None = None,
    refresh_schedule: bool | None = None,
) -> dict[str, object]:
    return sync_mlb_features(session, target_date, {"team"}, refresh_schedule)


def sync_mlb_pitcher_features(
    session: Session,
    target_date: date | None = None,
    refresh_schedule: bool | None = None,
) -> dict[str, object]:
    return sync_mlb_features(session, target_date, {"pitcher"}, refresh_schedule)


def sync_mlb_lineups(
    session: Session,
    target_date: date | None = None,
    refresh_schedule: bool | None = None,
) -> dict[str, object]:
    return sync_mlb_features(session, target_date, {"lineup"}, refresh_schedule)


def sync_mlb_bullpen_features(
    session: Session,
    target_date: date | None = None,
    refresh_schedule: bool | None = None,
) -> dict[str, object]:
    return sync_mlb_features(session, target_date, {"bullpen"}, refresh_schedule)


def sync_weather_features(
    session: Session,
    target_date: date | None = None,
    refresh_schedule: bool | None = None,
) -> dict[str, object]:
    return sync_mlb_features(session, target_date, {"weather"}, refresh_schedule)


def sync_travel_schedule_features(
    session: Session,
    target_date: date | None = None,
    refresh_schedule: bool | None = None,
) -> dict[str, object]:
    return sync_mlb_features(session, target_date, {"travel"}, refresh_schedule)


SOURCE_TABLE_MODELS = {
    "team_daily_features": TeamDailyFeature,
    "team_recent_features": TeamRecentFeature,
    "pitcher_daily_features": PitcherDailyFeature,
    "bullpen_daily_features": BullpenDailyFeature,
    "lineup_snapshots": LineupSnapshot,
    "injury_snapshots": InjurySnapshot,
    "weather_snapshots": WeatherSnapshot,
    "park_factor_snapshots": ParkFactorSnapshot,
    "travel_schedule_features": TravelScheduleFeature,
    "mlb_feature_snapshots": MlbFeatureSnapshot,
}


def _table_source_status(session: Session, table_name: str, model) -> dict[str, object]:
    statement = select(model).order_by(model.captured_at.desc()).limit(200)
    if model is MlbFeatureSnapshot:
        statement = (
            select(model)
            .where(MlbFeatureSnapshot.source == FEATURE_VERSION)
            .where(MlbFeatureSnapshot.mlb_game_id.is_not(None))
            .order_by(model.captured_at.desc())
            .limit(200)
        )
    rows = list(session.scalars(statement))
    counts: dict[str, int] = {}
    last_success = None
    last_error = None

    def timestamp(value: object) -> str | None:
        return ensure_aware_utc(value).isoformat() if isinstance(value, datetime) else None

    for row in rows:
        status = str(getattr(row, "source_status", None) or "available")
        counts[status] = counts.get(status, 0) + 1
        captured_at = getattr(row, "captured_at", None)
        if last_success is None and status in {"available", "partial"} and captured_at is not None:
            last_success = timestamp(captured_at)
        raw_payload = getattr(row, "raw_payload", None)
        if last_error is None and isinstance(raw_payload, dict) and raw_payload.get("error"):
            last_error = raw_payload.get("error")
    return {
        "table": table_name,
        "row_sample_count": len(rows),
        "latest_captured_at": timestamp(rows[0].captured_at) if rows else None,
        "last_successful_sync": last_success,
        "last_error": last_error,
        "status_counts": counts,
    }


def _latest_feature_sync_audit(session: Session) -> dict[str, object]:
    rows = list(
        session.scalars(
            select(MlbFeatureSnapshot)
            .where(MlbFeatureSnapshot.source.in_([FEATURE_VERSION, FEATURE_SYNC_AUDIT_SOURCE]))
            .order_by(MlbFeatureSnapshot.captured_at.desc(), MlbFeatureSnapshot.id.desc())
            .limit(50)
        )
    )
    latest_audit: dict[str, object] | None = None
    latest_errors: list[dict[str, object]] = []
    latest_attempted_at = None
    seen_error_keys: set[str] = set()
    for row in rows:
        row_features = row.features or {}
        sync_status = row_features.get("sync_status") if isinstance(row_features, dict) else None
        if not isinstance(sync_status, dict):
            continue
        attempted_at = sync_status.get("attempted_at")
        if latest_audit is None:
            latest_audit = sync_status
            latest_attempted_at = attempted_at
        if attempted_at != latest_attempted_at:
            continue
        errors = sync_status.get("errors")
        if isinstance(errors, list):
            for error in errors:
                if isinstance(error, dict):
                    error_key = repr(sorted(error.items()))
                    if error_key in seen_error_keys:
                        continue
                    seen_error_keys.add(error_key)
                    latest_errors.append(error)
    last_error = latest_errors[0] if latest_errors else None
    return {
        "last_attempted_sync": latest_audit.get("attempted_at") if latest_audit else None,
        "validation_status": latest_audit.get("validation_status") if latest_audit else None,
        "last_error": last_error,
        "latest_errors": latest_errors[:20],
    }


def _secret_configured(value: object) -> bool:
    if value is None:
        return False
    get_secret_value = getattr(value, "get_secret_value", None)
    if callable(get_secret_value):
        return bool(get_secret_value())
    return bool(value)


def source_status_report(session: Session) -> dict[str, object]:
    settings = get_settings()
    table_status = {
        table_name: _table_source_status(session, table_name, model)
        for table_name, model in SOURCE_TABLE_MODELS.items()
    }
    feature_audit = _latest_feature_sync_audit(session)
    last_feature_snapshot = table_status["mlb_feature_snapshots"]["latest_captured_at"]
    table_errors = {
        table_name: status["last_error"]
        for table_name, status in table_status.items()
        if status["last_error"] is not None
    }
    audit_errors = feature_audit["latest_errors"] if isinstance(feature_audit["latest_errors"], list) else []
    for error in audit_errors:
        if isinstance(error, dict):
            table = str(error.get("table") or "feature_sync")
            table_errors.setdefault(table, error)
    return {
        "feature_sync_enable_network_sources": settings.feature_sync_enable_network_sources,
        "mlb_stats_base_url": settings.mlb_stats_base_url,
        "open_meteo_base_url": settings.open_meteo_base_url,
        "pybaseball_available": pybaseball_available(),
        "advanced_public_stats_status": advanced_public_stats_status(),
        "public_sources_enabled": settings.feature_sync_enable_network_sources,
        "optional_injury_provider_configured": _secret_configured(settings.injury_provider_api_key),
        "optional_lineup_provider_configured": _secret_configured(settings.lineup_provider_api_key),
        "optional_weather_provider_configured": _secret_configured(settings.weather_provider_api_key),
        "last_successful_sync": {
            table_name: status["last_successful_sync"]
            for table_name, status in table_status.items()
        },
        "last_attempted_sync": feature_audit["last_attempted_sync"],
        "validation_status": feature_audit["validation_status"],
        "last_error": table_errors,
        "last_feature_sync_status": {
            "captured_at": last_feature_snapshot,
            "feature_version": FEATURE_VERSION,
            "last_attempted_sync": feature_audit["last_attempted_sync"],
            "validation_status": feature_audit["validation_status"],
            "last_error": feature_audit["last_error"],
        },
        "latest_errors": feature_audit["latest_errors"],
        "tables": table_status,
    }


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
