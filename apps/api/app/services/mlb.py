from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import MlbGame
from app.services.http_json import get_json
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
            scheduled_start = parse_datetime(game.get("gameDate"))
            if scheduled_start is None:
                continue

            existing = session.scalar(select(MlbGame).where(MlbGame.external_game_id == game_pk))
            row = existing or MlbGame(external_game_id=game_pk)
            row.home_team = home_team
            row.away_team = away_team
            row.scheduled_start = scheduled_start
            row.status = game.get("status", {}).get("detailedState") or game.get("status", {}).get("abstractGameState") or "scheduled"
            row.home_score = home.get("score")
            row.away_score = away.get("score")
            row.raw_payload = game
            session.add(row)
            count += 1

    session.commit()
    return count
