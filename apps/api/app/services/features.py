from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from math import radians, sin, cos, sqrt, atan2
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import KalshiMarket, MarketMapping, MlbGame, MlbFeatureSnapshot
from app.services.contracts import selected_team_from_ticker
from app.time_utils import ensure_aware_utc, get_dashboard_zone, today_eastern, utc_now

FEATURE_VERSION = "mature_mlb_features_v1"
MATURE_FEATURE_VERSION = FEATURE_VERSION
LEAGUE_AVG_FULL_GAME_RUNS = Decimal("4.35")
LEAGUE_AVG_FIRST_FIVE_RUNS = Decimal("2.15")
EARTH_RADIUS_MILES = Decimal("3958.8")


def _missing(component: str, reason: str = "no reliable source configured") -> dict[str, object]:
    return {"component": component, "source_status": "missing", "reason": reason}


def _available(component: str, values: dict[str, object] | None = None) -> dict[str, object]:
    return {"component": component, "source_status": "available", **(values or {})}


def _unavailable(component: str, reason: str) -> dict[str, object]:
    return {"component": component, "source_status": "unavailable", "reason": reason}


def _team_payload(game: MlbGame, side: str) -> dict[str, Any]:
    raw = game.raw_payload or {}
    teams = raw.get("teams") if isinstance(raw, dict) else None
    team = teams.get(side) if isinstance(teams, dict) else None
    return team if isinstance(team, dict) else {}


def _team_record(game: MlbGame, side: str) -> dict[str, object]:
    team = _team_payload(game, side)
    record = team.get("leagueRecord")
    if not isinstance(record, dict):
        return _missing(f"{side}_team_record", "MLB schedule payload did not include leagueRecord")
    wins = _int(record.get("wins"))
    losses = _int(record.get("losses"))
    pct = _decimal(record.get("pct"))
    if pct is None and wins is not None and losses is not None and wins + losses > 0:
        pct = (Decimal(wins) / Decimal(wins + losses)).quantize(Decimal("0.0001"))
    return _available(
        f"{side}_team_record",
        {
            "wins": wins,
            "losses": losses,
            "win_pct": _float(pct),
            "source": "mlb_stats_schedule",
        },
    )


def _probable_pitcher(game: MlbGame, side: str) -> dict[str, object]:
    team = _team_payload(game, side)
    pitcher = team.get("probablePitcher")
    if isinstance(pitcher, dict) and pitcher.get("fullName"):
        return _available(
            f"{side}_probable_starter",
            {
                "name": pitcher.get("fullName"),
                "id": pitcher.get("id"),
                "side": side,
                "source": "mlb_stats_schedule",
            },
        )
    return _missing(f"{side}_probable_starter", "MLB schedule did not include probablePitcher")


def _venue(game: MlbGame) -> dict[str, object]:
    raw = game.raw_payload or {}
    venue = raw.get("venue") if isinstance(raw, dict) else None
    if isinstance(venue, dict) and venue.get("name"):
        return _available(
            "venue",
            {
                "id": venue.get("id"),
                "name": venue.get("name"),
                "source": "mlb_stats_schedule",
            },
        )
    return _missing("venue", "MLB schedule payload did not include venue")


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.0001"))
    except Exception:
        return None


def _int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except Exception:
        return None


def _float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


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


def _rest_days(session: Session | None, game: MlbGame, team_code: str | None) -> int | None:
    if session is None or game.id is None or not team_code:
        return None
    start = ensure_aware_utc(game.scheduled_start)
    previous = session.scalar(
        select(MlbGame)
        .where(MlbGame.id != game.id)
        .where(MlbGame.scheduled_start < start)
        .where(MlbGame.scheduled_start >= start - timedelta(days=7))
        .where((MlbGame.home_abbreviation == team_code) | (MlbGame.away_abbreviation == team_code))
        .order_by(MlbGame.scheduled_start.desc())
        .limit(1)
    )
    if previous is None:
        return None
    delta = start.date() - ensure_aware_utc(previous.scheduled_start).date()
    return max(delta.days - 1, 0)


def _distance_miles(home: MlbGame | None, away: MlbGame | None) -> float | None:
    home_raw = home.raw_payload if home else None
    away_raw = away.raw_payload if away else None
    home_venue = home_raw.get("venue") if isinstance(home_raw, dict) else None
    away_venue = away_raw.get("venue") if isinstance(away_raw, dict) else None
    home_location = home_venue.get("location") if isinstance(home_venue, dict) else None
    away_location = away_venue.get("location") if isinstance(away_venue, dict) else None
    if not isinstance(home_location, dict) or not isinstance(away_location, dict):
        return None
    lat1 = _decimal(home_location.get("latitude"))
    lon1 = _decimal(home_location.get("longitude"))
    lat2 = _decimal(away_location.get("latitude"))
    lon2 = _decimal(away_location.get("longitude"))
    if None in {lat1, lon1, lat2, lon2}:
        return None
    d_lat = radians(float(lat2 - lat1))
    d_lon = radians(float(lon2 - lon1))
    a = sin(d_lat / 2) ** 2 + cos(radians(float(lat1))) * cos(radians(float(lat2))) * sin(d_lon / 2) ** 2
    return float(EARTH_RADIUS_MILES * Decimal(str(2 * atan2(sqrt(a), sqrt(1 - a)))))


def _component_score(module: dict[str, object]) -> Decimal:
    status = module.get("source_status")
    if status == "available":
        return Decimal("1.00")
    if status == "partial":
        return Decimal("0.50")
    return Decimal("0.00")


def _source_statuses(features: dict[str, object]) -> dict[str, object]:
    statuses: dict[str, object] = {}
    for key, value in features.items():
        if isinstance(value, dict) and "source_status" in value:
            statuses[key] = value["source_status"]
        elif isinstance(value, dict):
            nested = {
                nested_key: nested_value.get("source_status")
                for nested_key, nested_value in value.items()
                if isinstance(nested_value, dict) and "source_status" in nested_value
            }
            if nested:
                statuses[key] = nested
    return statuses


def _quality_score(features: dict[str, object]) -> Decimal:
    weighted_modules = {
        "game_context": Decimal("0.18"),
        "market_context": Decimal("0.18"),
        "team_strength_prior": Decimal("0.12"),
        "starter": Decimal("0.10"),
        "travel_schedule": Decimal("0.08"),
        "park_weather": Decimal("0.08"),
        "offense_season": Decimal("0.07"),
        "offense_recent": Decimal("0.05"),
        "bullpen": Decimal("0.05"),
        "lineup": Decimal("0.04"),
        "defense_catcher": Decimal("0.03"),
        "injuries": Decimal("0.02"),
    }
    quality = Decimal("0.30")
    for key, weight in weighted_modules.items():
        value = features.get(key)
        if isinstance(value, dict) and "source_status" in value:
            quality += _component_score(value) * weight
        elif isinstance(value, dict):
            nested_scores = [_component_score(v) for v in value.values() if isinstance(v, dict) and "source_status" in v]
            if nested_scores:
                quality += (sum(nested_scores) / Decimal(len(nested_scores))) * weight
    return min(max(quality, Decimal("0.1000")), Decimal("1.0000")).quantize(Decimal("0.0001"))


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
    minutes_to_start = int((scheduled_start - captured_at).total_seconds() / 60)
    home_record = _team_record(game, "home")
    away_record = _team_record(game, "away")
    home_rest = _rest_days(session, game, home_code)
    away_rest = _rest_days(session, game, away_code)

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
                "venue": _venue(game),
                "game_status": game.status,
                "day_night": _day_night(game),
                "doubleheader": bool((game.raw_payload or {}).get("doubleHeader")) if isinstance(game.raw_payload, dict) else False,
                "series_game_number": (game.raw_payload or {}).get("seriesGameNumber") if isinstance(game.raw_payload, dict) else None,
                "start_time_bucket": _time_bucket(minutes_to_start),
                "source": "mlb_stats_schedule",
            },
        ),
        "market_context": _available(
            "market_context",
            {
                "market_family": mapping.market_family or market.market_family,
                "ticker": market.ticker,
                "event_ticker": market.event_ticker,
                "side": "yes",
                "line_value": _float(mapping.line_value if mapping.line_value is not None else market.line_value),
                "selection_code": selected,
                "over_under_side": mapping.over_under_side or market.over_under_side,
                "inning_scope": mapping.inning_scope or market.inning_scope,
                "price": _float(market.implied_yes_ask if market.implied_yes_ask is not None else market.yes_ask),
                "yes_bid": _float(market.yes_bid),
                "yes_ask": _float(market.yes_ask),
                "best_yes_bid": _float(market.best_yes_bid),
                "implied_yes_ask": _float(market.implied_yes_ask),
                "time_to_start_minutes": minutes_to_start,
                "time_bucket": _time_bucket(minutes_to_start),
                "current_mark_timestamp": None,
                "fee_estimate": 0.0,
                "mapping_confidence": _float(mapping.confidence),
                "settlement_rule_status": mapping.settlement_rule_status or market.settlement_rule_status,
                "source": "kalshi_public_market_data",
            },
        ),
        "team_strength_prior": {
            "home": home_record,
            "away": away_record,
            "source_status": "available" if home_record["source_status"] == "available" and away_record["source_status"] == "available" else "partial",
            "league_average_full_game_runs": float(LEAGUE_AVG_FULL_GAME_RUNS),
            "league_average_first_five_runs": float(LEAGUE_AVG_FIRST_FIVE_RUNS),
            "early_season_shrinkage": 0.65,
        },
        "offense_season": _missing("offense_season", "advanced team batting adapter not configured in PR3c"),
        "offense_recent": _missing("offense_recent", "recent team batting adapter not configured in PR3c"),
        "starter": {
            "home": _probable_pitcher(game, "home"),
            "away": _probable_pitcher(game, "away"),
            "source_status": "partial",
            "starter_stats": _missing("starter_stats", "starter advanced-stat adapter not configured in PR3c"),
            "expected_workload": _missing("starter_workload", "pitch count history adapter not configured in PR3c"),
        },
        "bullpen": _missing("bullpen", "bullpen workload/quality adapter not configured in PR3c"),
        "defense_catcher": _missing("defense_catcher", "defense/catcher adapter not configured; umpire intentionally excluded"),
        "lineup": _missing("lineup", "confirmed lineup adapter not configured in PR3c"),
        "injuries": _missing("injuries", "reliable injury feed not configured in PR3c"),
        "park_weather": {
            **_missing("park_weather", "weather adapter not configured in PR3c"),
            "park": _venue(game),
            "weather_source_status": "missing",
            "roof_or_dome": None,
        },
        "travel_schedule": {
            "component": "travel_schedule",
            "source_status": "partial" if home_rest is not None or away_rest is not None else "missing",
            "home_rest_days": home_rest,
            "away_rest_days": away_rest,
            "travel_distance_estimate": None,
            "time_zone_change": None,
            "road_trip_home_stand_length": None,
            "getaway_day": False,
            "day_game_after_night_game": False,
            "doubleheader": bool((game.raw_payload or {}).get("doubleHeader")) if isinstance(game.raw_payload, dict) else False,
            "prior_extra_inning_game": None,
        },
    }
    data_quality = _quality_score(features)
    features["data_quality"] = float(data_quality)
    features["data_quality_summary"] = {
        "score": float(data_quality),
        "source_statuses": _source_statuses(features),
        "stale_data_flags": [],
        "context": "pregame" if minutes_to_start > 0 else "post_start",
    }
    features["source_statuses"] = _source_statuses(features)
    return features


def sync_mlb_features(session: Session, target_date: date | None = None) -> dict[str, object]:
    day = target_date or today_eastern()
    local_start = datetime.combine(day, time.min, tzinfo=get_dashboard_zone())
    start = ensure_aware_utc(local_start)
    end = start + timedelta(days=1)
    games = list(
        session.scalars(
            select(MlbGame)
            .where(MlbGame.scheduled_start >= start)
            .where(MlbGame.scheduled_start < end)
            .order_by(MlbGame.scheduled_start.asc())
        )
    )
    captured_at = utc_now()
    upserted = 0
    for game in games:
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
            .where(MlbFeatureSnapshot.source == "mlb_stats_schedule")
        )
        row = row or MlbFeatureSnapshot(mlb_game_id=game.id, target_date=day, source="mlb_stats_schedule")
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
        "source": "mlb_stats_schedule",
    }


def feature_coverage(session: Session, target_date: date | None = None) -> dict[str, object]:
    day = target_date or today_eastern()
    rows = list(
        session.scalars(
            select(MlbFeatureSnapshot)
            .where(MlbFeatureSnapshot.target_date == day)
            .order_by(MlbFeatureSnapshot.id.asc())
        )
    )
    avg_quality = None
    if rows:
        avg_quality = float(sum((row.data_quality or Decimal("0")) for row in rows) / Decimal(len(rows)))
    return {
        "date": day.isoformat(),
        "feature_version": FEATURE_VERSION,
        "snapshot_count": len(rows),
        "data_quality_avg": avg_quality,
        "items": [
            {
                "game_id": row.mlb_game_id,
                "source": row.source,
                "captured_at": row.captured_at.isoformat(),
                "data_quality": _float(row.data_quality),
                "source_statuses": row.source_statuses,
            }
            for row in rows[:200]
        ],
    }
