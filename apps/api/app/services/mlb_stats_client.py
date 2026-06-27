from __future__ import annotations

from datetime import date
from typing import Any

from app.config import get_settings
from app.services.http_json import get_json


class MLBStatsClient:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or get_settings().mlb_stats_base_url).rstrip("/")

    def get_schedule(
        self,
        target_date: date | None = None,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        hydrate: str = "probablePitcher(note),team,venue,linescore",
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
        return get_json(f"{self.base_url}/game/{game_pk}/feed/live", params={})

    def get_boxscore(self, game_pk: str) -> dict[str, Any]:
        return get_json(f"{self.base_url}/game/{game_pk}/boxscore", params={})

    def get_linescore(self, game_pk: str) -> dict[str, Any]:
        return get_json(f"{self.base_url}/game/{game_pk}/linescore", params={})
