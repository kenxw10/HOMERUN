from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.models import KalshiMarket, MarketMapping, MlbGame
from app.services.contracts import selected_team_from_ticker

FEATURE_VERSION = "mlb_features_v1"


def _missing(component: str, reason: str = "no reliable source configured") -> dict[str, object]:
    return {"component": component, "source_status": "missing", "reason": reason}


def _team_payload(game: MlbGame, side: str) -> dict[str, Any]:
    raw = game.raw_payload or {}
    teams = raw.get("teams") if isinstance(raw, dict) else None
    team = teams.get(side) if isinstance(teams, dict) else None
    return team if isinstance(team, dict) else {}


def _probable_pitcher(game: MlbGame, side: str) -> dict[str, object]:
    team = _team_payload(game, side)
    pitcher = team.get("probablePitcher")
    if isinstance(pitcher, dict) and pitcher.get("fullName"):
        return {
            "source_status": "available",
            "name": pitcher.get("fullName"),
            "id": pitcher.get("id"),
            "side": side,
        }
    return _missing(f"{side}_probable_starter", "MLB schedule did not include probablePitcher")


def build_feature_snapshot(game: MlbGame, market: KalshiMarket, mapping: MarketMapping) -> dict[str, object]:
    selected = selected_team_from_ticker(market.ticker)
    home_code = (game.home_abbreviation or "").upper()
    away_code = (game.away_abbreviation or "").upper()
    selected_is_home = selected == home_code if selected else None
    selected_is_away = selected == away_code if selected else None

    available_components = 0
    total_components = 8
    if selected:
        available_components += 1
    if home_code and away_code:
        available_components += 1
    if game.scheduled_start:
        available_components += 1
    if mapping.confidence is not None:
        available_components += 1
    if market.implied_yes_ask is not None or market.yes_ask is not None:
        available_components += 1
    if _probable_pitcher(game, "home").get("source_status") == "available":
        available_components += 1
    if _probable_pitcher(game, "away").get("source_status") == "available":
        available_components += 1
    if game.raw_payload:
        available_components += 1

    data_quality = Decimal(available_components) / Decimal(total_components)
    data_quality = min(max(data_quality, Decimal("0.10")), Decimal("1.00"))

    return {
        "feature_version": FEATURE_VERSION,
        "market_family": "full_game_winner",
        "selected_team_code": selected,
        "selected_is_home": selected_is_home,
        "selected_is_away": selected_is_away,
        "home_team": game.home_team,
        "away_team": game.away_team,
        "home_abbreviation": home_code,
        "away_abbreviation": away_code,
        "scheduled_start": game.scheduled_start.isoformat(),
        "game_status": game.status,
        "mapping_confidence": float(mapping.confidence or Decimal("0")),
        "mapping_status": mapping.mapping_status,
        "market_status": market.status,
        "best_yes_bid": float(market.best_yes_bid) if market.best_yes_bid is not None else None,
        "implied_yes_ask": float(market.implied_yes_ask) if market.implied_yes_ask is not None else None,
        "yes_ask": float(market.yes_ask) if market.yes_ask is not None else None,
        "probable_starters": {
            "home": _probable_pitcher(game, "home"),
            "away": _probable_pitcher(game, "away"),
        },
        "starter_season_stats": _missing("starter_season_stats"),
        "starter_recent_form_last_3": _missing("starter_recent_form_last_3"),
        "starter_recent_form_last_5": _missing("starter_recent_form_last_5"),
        "team_season_hitting": _missing("team_season_hitting"),
        "team_season_pitching": _missing("team_season_pitching"),
        "team_recent_hitting_form": _missing("team_recent_hitting_form"),
        "team_recent_pitching_bullpen_proxy": _missing("team_recent_pitching_bullpen_proxy"),
        "handedness_splits": _missing("handedness_splits"),
        "lineup_confirmation": _missing("lineup_confirmation"),
        "weather": _missing("weather"),
        "injuries": _missing("injuries"),
        "data_quality": float(data_quality.quantize(Decimal("0.0001"))),
    }
