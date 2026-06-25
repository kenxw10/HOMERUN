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
        params={"sportId": 1, "date": target.isoformat(), "hydrate": "team,linescore"},
    )


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
            row.raw_payload = game
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
                row.raw_payload = game
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
