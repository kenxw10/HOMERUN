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
    actual_contract_display: str | None = None
    normalized_equivalent_display: str | None = None
    display_title: str | None = None
    display_subtitle: str | None = None
    raw_ticker_display: str | None = None


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


def _team_display(game: MlbGame | None, code: str | None) -> str | None:
    if game is None or not code:
        return code
    normalized = code.upper()
    if normalized == (game.home_abbreviation or "").upper():
        return game.home_team or game.home_abbreviation
    if normalized == (game.away_abbreviation or "").upper():
        return game.away_team or game.away_abbreviation
    return code


def _opponent_code(game: MlbGame | None, code: str | None) -> str | None:
    if game is None or not code:
        return None
    normalized = code.upper()
    home = (game.home_abbreviation or "").upper()
    away = (game.away_abbreviation or "").upper()
    if normalized == home:
        return away or None
    if normalized == away:
        return home or None
    return None


def _scope_display(market_type: str | None) -> str:
    return "first 5 innings" if (market_type or "").startswith("first_five") else "full game"


def contract_labels(
    *,
    game: MlbGame | None,
    market: KalshiMarket | None,
    market_ticker: str,
    market_type: str | None,
    selection_code: str | None = None,
    contract_side: str = "yes",
) -> ContractLabels:
    ticker = market_ticker.upper()
    matchup = matchup_display(game)
    title = (market.title if market else None) or ticker
    subtitle = (market.subtitle if market else None) or (market.yes_subtitle if market else None)
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

    def fmt_total_line() -> str:
        if line_value is None:
            return ""
        return f"{abs(float(line_value)):g}"

    def selection_with_line() -> str:
        line = fmt_line()
        return f"{selection} {line}".strip()

    def total_selection() -> str:
        side = str(over_under_side or selection).upper()
        if side in {"O", "OVER"}:
            side = "OVER"
        elif side in {"U", "UNDER"}:
            side = "UNDER"
        line = fmt_total_line()
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
        market_display = f"{matchup} - {family_label} - {selection_label}"
        side = contract_side.upper()
        actual_display = market_display
        normalized_display: str | None = None
        scope = _scope_display(market_type)
        if market_type in {FULL_GAME_SPREAD, FIRST_FIVE_SPREAD} and line_value is not None:
            team = _team_display(game, selection) or selection
            actual_display = f"{side} on {team} {fmt_line()} {scope}".upper()
            if contract_side.lower() == "no":
                opponent = _opponent_code(game, selection)
                opponent_display = _team_display(game, opponent) or opponent
                if opponent_display:
                    equivalent_line = -float(line_value)
                    equivalent = f"+{equivalent_line:g}" if equivalent_line > 0 else f"{equivalent_line:g}"
                    normalized_display = f"{opponent_display} {equivalent} {scope} equivalent".upper()
            else:
                normalized_display = f"{team} {fmt_line()} {scope}".upper()
        elif market_type in {FULL_GAME_WINNER, FIRST_FIVE_WINNER}:
            team = _team_display(game, selection) or selection
            actual_display = f"{side} on {team} {scope} winner".upper()
            if contract_side.lower() == "no":
                if market_type == FIRST_FIVE_WINNER:
                    if selection.upper() == "TIE":
                        home = _team_display(game, getattr(game, "home_abbreviation", None)) or getattr(game, "home_team", None)
                        away = _team_display(game, getattr(game, "away_abbreviation", None)) or getattr(game, "away_team", None)
                        if home and away:
                            normalized_display = f"{home} or {away} win first 5 innings equivalent".upper()
                        else:
                            normalized_display = "EITHER TEAM WINS FIRST 5 INNINGS EQUIVALENT"
                    else:
                        normalized_display = f"{team} does not win first 5 innings (opponent or tie)".upper()
                else:
                    opponent = _opponent_code(game, selection)
                    opponent_display = _team_display(game, opponent) or opponent
                    if opponent_display:
                        normalized_display = f"{opponent_display} full game winner equivalent".upper()
            else:
                normalized_display = f"{team} {scope} winner".upper()
        elif market_type in {FULL_GAME_TOTAL, FIRST_FIVE_TOTAL}:
            total_label = total_selection()
            actual_display = f"{side} on {total_label} {scope}".upper()
            if contract_side.lower() == "no":
                line = fmt_total_line()
                total_side = str(over_under_side or selection).upper()
                if total_side in {"O", "OVER"}:
                    normalized_display = f"UNDER {line} {scope} equivalent".upper()
                elif total_side in {"U", "UNDER"}:
                    normalized_display = f"OVER {line} {scope} equivalent".upper()
                else:
                    normalized_display = f"NOT {total_label} {scope}".upper()
            else:
                normalized_display = f"{total_label} {scope}".upper()
        return ContractLabels(
            market_display=market_display,
            selection_display=selection_label,
            matchup_display=matchup,
            contract_display=market_display,
            actual_contract_display=actual_display,
            normalized_equivalent_display=normalized_display,
            display_title=title,
            display_subtitle=subtitle,
            raw_ticker_display=ticker,
        )

    fallback = ticker if title.upper() == ticker else f"{ticker} - {title}".upper()
    return ContractLabels(
        market_display=fallback,
        selection_display=selection,
        matchup_display=matchup or "UNKNOWN MATCHUP",
        contract_display=fallback,
        display_title=title,
        display_subtitle=subtitle,
        raw_ticker_display=ticker,
    )


def market_type_from_ticker(ticker: str | None, inferred: str | None = None) -> str:
    upper = (ticker or "").upper()
    for prefix, family in sorted(MARKET_FAMILY_PREFIXES.items(), key=lambda item: len(item[0]), reverse=True):
        if upper.startswith(f"{prefix}-"):
            return family
    if inferred == "full_game_moneyline":
        return SUPPORTED_MARKET_FAMILY
    return inferred or "unknown"
