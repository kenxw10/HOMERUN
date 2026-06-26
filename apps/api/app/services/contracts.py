from __future__ import annotations

from dataclasses import dataclass
import re

from app.models import KalshiMarket, MlbGame


SUPPORTED_MARKET_FAMILY = "full_game_winner"
FULL_GAME_WINNER = "full_game_winner"
FULL_GAME_SPREAD = "full_game_spread"
FULL_GAME_TOTAL = "full_game_total"
FIRST_FIVE_WINNER = "first_five_winner"
FIRST_FIVE_SPREAD = "first_five_spread"
FIRST_FIVE_TOTAL = "first_five_total"
PAPER_SUPPORTED_MARKET_FAMILIES = {
    FULL_GAME_WINNER,
    FULL_GAME_SPREAD,
    FULL_GAME_TOTAL,
    FIRST_FIVE_WINNER,
    FIRST_FIVE_SPREAD,
    FIRST_FIVE_TOTAL,
}
MARKET_FAMILY_PREFIXES = {
    "KXMLBGAME": FULL_GAME_WINNER,
    "KXMLBSPREAD": FULL_GAME_SPREAD,
    "KXMLBTOTAL": FULL_GAME_TOTAL,
    "KXMLBF5": FIRST_FIVE_WINNER,
    "KXMLBF5SPREAD": FIRST_FIVE_SPREAD,
    "KXMLBF5TOTAL": FIRST_FIVE_TOTAL,
}


@dataclass(frozen=True)
class ContractLabels:
    market_display: str
    selection_display: str
    matchup_display: str
    contract_display: str


def selected_team_from_ticker(ticker: str | None) -> str | None:
    if not ticker or "-" not in ticker:
        return None
    parts = ticker.upper().split("-")
    selected_part = parts[-1]
    if len(parts) >= 3 and re.fullmatch(r"[+-]?\d+(?:\.\d+)?", selected_part):
        selected_part = parts[-2]
    else:
        compact_line = re.match(r"^([A-Z0-9]{2,5})[+-]\d+(?:\.\d+)?$", selected_part)
        if compact_line:
            selected_part = compact_line.group(1)
    selected = re.sub(r"[^A-Za-z0-9]", "", selected_part).upper()
    return selected or None


def game_team_codes(game: MlbGame | None) -> set[str]:
    if game is None:
        return set()
    return {
        code
        for code in ((game.home_abbreviation or "").upper(), (game.away_abbreviation or "").upper())
        if code
    }


def has_trusted_selection(game: MlbGame | None, market_ticker: str | None) -> bool:
    selected = selected_team_from_ticker(market_ticker)
    team_codes = game_team_codes(game)
    return selected is not None and bool(team_codes) and selected in team_codes


def _normalized_selection_code(selection_code: str | None) -> str | None:
    if selection_code is None:
        return None
    selected = re.sub(r"[^A-Za-z0-9]", "", selection_code).upper()
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
    selection_code: str | None = None,
) -> ContractLabels:
    ticker = market_ticker.upper()
    matchup = matchup_display(game)
    title = (market.title if market else None) or ticker
    market_type = market_type_from_ticker(ticker, market_type)
    line_value = getattr(market, "line_value", None)
    over_under_side = getattr(market, "over_under_side", None)
    parsed_selection = _normalized_selection_code(selection_code or getattr(market, "selection_code", None))
    ticker_selection = selected_team_from_ticker(ticker) or "UNKNOWN"
    if market_type in {FULL_GAME_WINNER, FULL_GAME_SPREAD, FIRST_FIVE_WINNER, FIRST_FIVE_SPREAD}:
        selection = parsed_selection or ticker_selection
    else:
        selection = ticker_selection

    def fmt_line() -> str:
        if line_value is None:
            return ""
        value = float(line_value)
        if value > 0:
            return f"+{value:g}"
        return f"{value:g}"

    def selection_with_line() -> str:
        line = fmt_line()
        return f"{selection} {line}".strip()

    def total_selection() -> str:
        side = str(over_under_side or selection).upper()
        if side in {"O", "OVER"}:
            side = "OVER"
        elif side in {"U", "UNDER"}:
            side = "UNDER"
        line = fmt_line()
        return f"{side} {line}".strip()

    family_labels = {
        FULL_GAME_WINNER: ("FULL GAME WINNER", selection),
        FULL_GAME_SPREAD: ("FULL GAME SPREAD", selection_with_line()),
        FULL_GAME_TOTAL: ("FULL GAME TOTAL", total_selection()),
        FIRST_FIVE_WINNER: ("FIRST FIVE WINNER", selection),
        FIRST_FIVE_SPREAD: ("FIRST FIVE SPREAD", selection_with_line()),
        FIRST_FIVE_TOTAL: ("FIRST FIVE TOTAL", total_selection()),
    }
    if market_type in family_labels and matchup:
        family_label, selection_label = family_labels[market_type]
        market_display = f"{family_label} - {matchup} - {selection_label}"
        return ContractLabels(
            market_display=market_display,
            selection_display=selection_label,
            matchup_display=matchup,
            contract_display=market_display,
        )

    fallback = ticker if title.upper() == ticker else f"{ticker} - {title}".upper()
    return ContractLabels(
        market_display=fallback,
        selection_display=selection,
        matchup_display=matchup or "UNKNOWN MATCHUP",
        contract_display=fallback,
    )


def market_type_from_ticker(ticker: str | None, inferred: str | None = None) -> str:
    upper = (ticker or "").upper()
    for prefix, family in sorted(MARKET_FAMILY_PREFIXES.items(), key=lambda item: len(item[0]), reverse=True):
        if upper.startswith(f"{prefix}-"):
            return family
    if inferred == "full_game_moneyline":
        return SUPPORTED_MARKET_FAMILY
    return inferred or "unknown"
