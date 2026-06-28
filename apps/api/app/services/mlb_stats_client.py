from __future__ import annotations

from datetime import date
from typing import Any

from app.config import get_settings
from app.services.http_json import get_json


class MLBStatsClient:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or get_settings().mlb_stats_base_url).rstrip("/")
        self.live_feed_base_url = (
            f"{self.base_url.removesuffix('/api/v1')}/api/v1.1"
            if self.base_url.endswith("/api/v1")
            else self.base_url
        )

    def get_schedule(
        self,
        target_date: date | None = None,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        hydrate: str = "probablePitcher(note),team,venue",
    ) -> dict[str, Any]:
        params: dict[str, object] = {"sportId": 1, "hydrate": hydrate}
        if target_date is not None:
            params["date"] = target_date.isoformat()
        else:
            if start_date is not None:
                params["startDate"] = start_date.isoformat()
            if end_date is not None:
                params["endDate"] = end_date.isoformat()
        return get_json(f"{self.base_url}/schedule", params=params)

    def get_game_feed(self, game_pk: str) -> dict[str, Any]:
        return get_json(f"{self.live_feed_base_url}/game/{game_pk}/feed/live", params={})

    def get_boxscore(self, game_pk: str) -> dict[str, Any]:
        return get_json(f"{self.base_url}/game/{game_pk}/boxscore", params={})

    def get_game_boxscore(self, game_pk: str) -> dict[str, Any]:
        return self.get_boxscore(game_pk)

    def get_linescore(self, game_pk: str) -> dict[str, Any]:
        return get_json(f"{self.base_url}/game/{game_pk}/linescore", params={})

    def get_pitcher_season_stats(self, person_id: str | int, season: int) -> dict[str, Any]:
        return get_json(
            f"{self.base_url}/people/{person_id}/stats",
            params={"stats": "season", "group": "pitching", "season": season},
        )

    def get_pitcher_game_log_stats(self, person_id: str | int, season: int) -> dict[str, Any]:
        return get_json(
            f"{self.base_url}/people/{person_id}/stats",
            params={"stats": "gameLog", "group": "pitching", "season": season},
        )

    def get_team_season_stats(self, group: str, season: int) -> dict[str, Any]:
        return get_json(
            f"{self.base_url}/teams/stats",
            params={"stats": "season", "group": group, "season": season, "sportId": 1},
        )

    def get_team_game_log_stats(self, team_id: str | int, group: str, season: int) -> dict[str, Any]:
        return get_json(
            f"{self.base_url}/teams/{team_id}/stats",
            params={"stats": "gameLog", "group": group, "season": season, "sportId": 1},
        )

    def get_team_stat_splits(
        self,
        team_id: str | int,
        group: str,
        season: int,
        sitCodes: str = "vl,vr",
    ) -> dict[str, Any]:
        return get_json(
            f"{self.base_url}/teams/{team_id}/stats",
            params={"stats": "statSplits", "group": group, "season": season, "sportId": 1, "sitCodes": sitCodes},
        )
