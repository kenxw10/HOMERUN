from __future__ import annotations

from dataclasses import dataclass
import re

from app.models import KalshiMarket, MlbGame


SUPPORTED_MARKET_FAMILY = "full_game_winner"


@dataclass(frozen=True)
class ContractLabels:
    market_display: str
    selection_display: str
    matchup_display: str
    contract_display: str


def selected_team_from_ticker(ticker: str | None) -> str | None:
    if not ticker or "-" not in ticker:
        return None
    selected = re.sub(r"[^A-Za-z0-9]", "", ticker.rsplit("-", 1)[-1]).upper()
    return selected or None


def matchup_display(game: MlbGame | None) -> str | None:
    if game is None:
        return None
    away = (game.away_abbreviation or game.away_team or "AWAY").upper()
    home = (game.home_abbreviation or game.home_team or "HOME").upper()
    return f"{away} @ {home}"


def contract_labels(
    *,
    game: MlbGame | None,
    market: KalshiMarket | None,
    market_ticker: str,
    market_type: str | None,
) -> ContractLabels:
    ticker = market_ticker.upper()
    selection = selected_team_from_ticker(ticker) or "UNKNOWN"
    matchup = matchup_display(game)
    title = (market.title if market else None) or ticker

    if market_type == SUPPORTED_MARKET_FAMILY and matchup:
        market_display = f"FULL GAME WINNER · {matchup} · {selection}"
        return ContractLabels(
            market_display=market_display,
            selection_display=selection,
            matchup_display=matchup,
            contract_display=market_display,
        )

    fallback = ticker if title.upper() == ticker else f"{ticker} · {title}".upper()
    return ContractLabels(
        market_display=fallback,
        selection_display=selection,
        matchup_display=matchup or "UNKNOWN MATCHUP",
        contract_display=fallback,
    )


def market_type_from_ticker(ticker: str | None, inferred: str | None = None) -> str:
    if ticker and ticker.upper().startswith("KXMLBGAME-"):
        return SUPPORTED_MARKET_FAMILY
    if inferred == "full_game_moneyline":
        return SUPPORTED_MARKET_FAMILY
    return inferred or "unknown"
