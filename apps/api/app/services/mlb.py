from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import MlbGame
from app.services.http_json import get_json
from app.services.kalshi_mlb_resolver import normalize_team_abbreviation
from app.time_utils import parse_datetime, today_eastern


def fetch_schedule(target_date: date | None = None) -> dict[str, Any]:
    target = target_date or today_eastern()
    settings = get_settings()
    return get_json(
        f"{settings.mlb_stats_base_url.rstrip('/')}/schedule",
        params={"sportId": 1, "date": target.isoformat(), "hydrate": "probablePitcher(note),team,venue,linescore"},
    )


def _schedule_sides_missing_probables(payload: dict[str, Any]) -> set[str]:
    teams = payload.get("teams")
    if not isinstance(teams, dict):
        return set()
    missing: set[str] = set()
    for side in ("home", "away"):
        team = teams.get(side)
        if not isinstance(team, dict):
            continue
        probable = team.get("probablePitcher")
        if not isinstance(probable, dict) or not probable.get("id"):
            missing.add(side)
    return missing


def _drop_stale_starter_cache(merged: dict[str, Any], missing_sides: set[str]) -> dict[str, Any]:
    if not missing_sides:
        return merged

    teams = merged.get("teams")
    if isinstance(teams, dict):
        teams = dict(teams)
        for side in missing_sides:
            team = teams.get(side)
            if isinstance(team, dict):
                team = dict(team)
                team.pop("probablePitcher", None)
                teams[side] = team
        merged["teams"] = teams

    hydration = merged.get("homerun_starter_hydration")
    if isinstance(hydration, dict):
        hydration = dict(hydration)
        for side in missing_sides:
            hydration.pop(side, None)
        if hydration:
            merged["homerun_starter_hydration"] = hydration
        else:
            merged.pop("homerun_starter_hydration", None)

    game_data = merged.get("gameData")
    if isinstance(game_data, dict):
        game_data = dict(game_data)
        probables = game_data.get("probablePitchers")
        if isinstance(probables, dict):
            probables = dict(probables)
            for side in missing_sides:
                probables.pop(side, None)
            if probables:
                game_data["probablePitchers"] = probables
            else:
                game_data.pop("probablePitchers", None)
        merged["gameData"] = game_data

    live_data = merged.get("liveData")
    boxscore = live_data.get("boxscore") if isinstance(live_data, dict) else None
    box_teams = boxscore.get("teams") if isinstance(boxscore, dict) else None
    if isinstance(live_data, dict) and isinstance(boxscore, dict) and isinstance(box_teams, dict):
        live_data = dict(live_data)
        boxscore = dict(boxscore)
        box_teams = dict(box_teams)
        for side in missing_sides:
            box_team = box_teams.get(side)
            if isinstance(box_team, dict):
                box_team = dict(box_team)
                box_team.pop("pitchers", None)
                box_teams[side] = box_team
        boxscore["teams"] = box_teams
        live_data["boxscore"] = boxscore
        merged["liveData"] = live_data

    return merged


def _merged_game_payload(existing: MlbGame | None, payload: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing.raw_payload or {}) if existing is not None and isinstance(existing.raw_payload, dict) else {}
    merged.update(payload)
    return _drop_stale_starter_cache(merged, _schedule_sides_missing_probables(payload))


def sync_schedule(session: Session, target_date: date | None = None) -> int:
    payload = fetch_schedule(target_date)
    count = 0
    for schedule_date in payload.get("dates", []):
        for game in schedule_date.get("games", []):
            game_pk = str(game.get("gamePk"))
            if not game_pk:
                continue
            teams = game.get("teams", {})
            home = teams.get("home", {})
            away = teams.get("away", {})
            home_team = home.get("team", {}).get("name") or "UNKNOWN HOME"
            away_team = away.get("team", {}).get("name") or "UNKNOWN AWAY"
            home_abbreviation = home.get("team", {}).get("abbreviation")
            away_abbreviation = away.get("team", {}).get("abbreviation")
            scheduled_start = parse_datetime(game.get("gameDate"))
            if scheduled_start is None:
                continue

            existing = session.scalar(select(MlbGame).where(MlbGame.external_game_id == game_pk))
            row = existing or MlbGame(external_game_id=game_pk)
            row.home_team = home_team
            row.away_team = away_team
            row.home_abbreviation = normalize_team_abbreviation(home_team, home_abbreviation)
            row.away_abbreviation = normalize_team_abbreviation(away_team, away_abbreviation)
            row.scheduled_start = scheduled_start
            row.status = game.get("status", {}).get("detailedState") or game.get("status", {}).get("abstractGameState") or "scheduled"
            row.home_score = home.get("score")
            row.away_score = away.get("score")
            row.raw_payload = _merged_game_payload(existing, game)
            session.add(row)
            count += 1

    session.commit()
    return count


def sync_results(session: Session, target_date: date | None = None) -> dict[str, object]:
    target_dates = [target_date] if target_date else [today_eastern() - timedelta(days=1), today_eastern()]
    updated = 0
    missing = 0
    final = 0

    for day in target_dates:
        payload = fetch_schedule(day)
        for schedule_date in payload.get("dates", []):
            for game in schedule_date.get("games", []):
                game_pk = str(game.get("gamePk"))
                if not game_pk:
                    continue
                row = session.scalar(select(MlbGame).where(MlbGame.external_game_id == game_pk))
                if row is None:
                    missing += 1
                    continue

                teams = game.get("teams", {})
                home = teams.get("home", {})
                away = teams.get("away", {})
                status = game.get("status", {})
                row.status = status.get("detailedState") or status.get("abstractGameState") or row.status
                row.home_score = home.get("score")
                row.away_score = away.get("score")
                row.raw_payload = _merged_game_payload(row, game)
                session.add(row)
                updated += 1
                if str(status.get("abstractGameState") or row.status).lower() == "final":
                    final += 1

    session.commit()
    return {
        "target_dates": [day.isoformat() for day in target_dates],
        "games_updated": updated,
        "missing_games": missing,
        "final_games": final,
    }
