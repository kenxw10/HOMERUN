from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from app.models import MlbGame
from app.services.contracts import (
    FIRST_FIVE_SPREAD,
    FIRST_FIVE_TOTAL,
    FIRST_FIVE_WINNER,
    FULL_GAME_SPREAD,
    FULL_GAME_TOTAL,
    FULL_GAME_WINNER,
)

EXPOSURE_TAXONOMY_VERSION = "pr3s_exposure_taxonomy_v1"
LINE_CLASSIFICATION_POLICY_VERSION = "pr3s_kalshi_ladder_line_class_v1"

SPREAD_FAMILIES = {FULL_GAME_SPREAD, FIRST_FIVE_SPREAD}
TOTAL_FAMILIES = {FULL_GAME_TOTAL, FIRST_FIVE_TOTAL}
WINNER_FAMILIES = {FULL_GAME_WINNER, FIRST_FIVE_WINNER}
LINE_FAMILIES = SPREAD_FAMILIES | TOTAL_FAMILIES


@dataclass(frozen=True)
class ExposureTaxonomy:
    economic_exposure_label: str | None
    economic_exposure_key: str | None
    economic_exposure_family: str | None
    economic_exposure_scope: str | None
    economic_exposure_direction: str | None
    economic_exposure_team: str | None
    economic_exposure_line: Decimal | None
    contract_mechanics_label: str | None
    concept_cluster_key: str | None
    same_game_concept_cluster_key: str | None
    exposure_taxonomy_version: str = EXPOSURE_TAXONOMY_VERSION

    def as_dict(self) -> dict[str, object]:
        return {
            "economic_exposure_label": self.economic_exposure_label,
            "economic_exposure_key": self.economic_exposure_key,
            "economic_exposure_family": self.economic_exposure_family,
            "economic_exposure_scope": self.economic_exposure_scope,
            "economic_exposure_direction": self.economic_exposure_direction,
            "economic_exposure_team": self.economic_exposure_team,
            "economic_exposure_line": _decimal_text(self.economic_exposure_line),
            "contract_mechanics_label": self.contract_mechanics_label,
            "concept_cluster_key": self.concept_cluster_key,
            "same_game_concept_cluster_key": self.same_game_concept_cluster_key,
            "exposure_taxonomy_version": self.exposure_taxonomy_version,
        }


@dataclass(frozen=True)
class LineClassification:
    line_class: str
    line_class_reason: str
    line_ladder_rank: int | None
    line_ladder_distance_from_central: int | None
    line_ladder_size: int
    line_classification_policy_version: str = LINE_CLASSIFICATION_POLICY_VERSION

    def as_dict(self) -> dict[str, object]:
        return {
            "line_class": self.line_class,
            "line_class_reason": self.line_class_reason,
            "line_ladder_rank": self.line_ladder_rank,
            "line_ladder_distance_from_central": self.line_ladder_distance_from_central,
            "line_ladder_size": self.line_ladder_size,
            "line_classification_policy_version": self.line_classification_policy_version,
        }


def exposure_taxonomy_for_candidate(
    *,
    game: MlbGame,
    market_family: str | None,
    selection_code: str | None,
    line_value: Decimal | None,
    over_under_side: str | None,
    contract_side: str | None,
    contract_mechanics_label: str | None,
    spread_verification: Any | None = None,
) -> ExposureTaxonomy:
    family_key = str(market_family or "unknown")
    scope = _scope_for_family(family_key)
    side = str(contract_side or "").lower()
    selected_team = _clean_team_code(selection_code)
    line = _decimal(line_value)

    exposure_family: str | None = None
    direction: str | None = None
    exposure_team: str | None = None
    exposure_line: Decimal | None = line
    label: str | None = None

    if family_key in TOTAL_FAMILIES:
        exposure_family = "total"
        base_direction = _normalize_total_side(over_under_side or selection_code)
        direction = _opposite_total_direction(base_direction) if side == "no" else base_direction
        label = _total_label(direction, line, scope)
    elif family_key in WINNER_FAMILIES:
        exposure_family = "winner"
        if side == "no" and family_key == FULL_GAME_WINNER:
            opponent = _opponent_code(game, selected_team)
            exposure_team = opponent or selected_team
            direction = "win" if opponent else "not_win"
        elif side == "no" and family_key == FIRST_FIVE_WINNER and selected_team == "TIE":
            direction = "either_team_win"
            exposure_team = None
        elif side == "no":
            direction = "not_win"
            exposure_team = selected_team
        else:
            direction = "win"
            exposure_team = selected_team
        label = _winner_label(exposure_team, direction, scope)
        exposure_line = None
    elif family_key in SPREAD_FAMILIES:
        exposure_family = "spread"
        exposure_team = selected_team
        direction = "cover"
        if side == "no":
            opponent = _opponent_code(game, selected_team)
            if _spread_no_is_safe_complement(spread_verification) and opponent is not None and line is not None:
                exposure_team = opponent
                exposure_line = -line
                direction = "cover"
            else:
                direction = "not_cover"
        label = _spread_label(exposure_team, exposure_line, direction, scope)
    else:
        exposure_family = "unknown"
        direction = "unknown"
        exposure_team = selected_team
        label = contract_mechanics_label

    exposure_key = _exposure_key(
        family=exposure_family,
        scope=scope,
        direction=direction,
        team=exposure_team,
        line=exposure_line,
    )
    concept_cluster_key = _concept_cluster_key(exposure_family, direction)
    same_game_concept_cluster_key = _same_game_concept_cluster_key(
        game_id=game.id,
        concept_cluster_key=concept_cluster_key,
        team=exposure_team,
    )
    return ExposureTaxonomy(
        economic_exposure_label=label.upper() if label else None,
        economic_exposure_key=exposure_key,
        economic_exposure_family=exposure_family,
        economic_exposure_scope=scope,
        economic_exposure_direction=direction,
        economic_exposure_team=exposure_team,
        economic_exposure_line=exposure_line,
        contract_mechanics_label=contract_mechanics_label,
        concept_cluster_key=concept_cluster_key,
        same_game_concept_cluster_key=same_game_concept_cluster_key,
    )


def line_classification_for_ladder(
    *,
    market_family: str | None,
    line_value: Decimal | None,
    ladder_lines: Iterable[Decimal | None],
) -> LineClassification:
    family = str(market_family or "")
    if family not in LINE_FAMILIES:
        return LineClassification(
            line_class="not_applicable",
            line_class_reason="market_family_has_no_line_ladder",
            line_ladder_rank=None,
            line_ladder_distance_from_central=None,
            line_ladder_size=0,
        )
    line = _decimal(line_value)
    if line is None:
        return LineClassification(
            line_class="unclassified",
            line_class_reason="missing_line",
            line_ladder_rank=None,
            line_ladder_distance_from_central=None,
            line_ladder_size=0,
        )
    unique_lines = sorted({_decimal(value) for value in ladder_lines if _decimal(value) is not None})
    ladder_size = len(unique_lines)
    if ladder_size < 3 or line not in unique_lines:
        return LineClassification(
            line_class="unclassified",
            line_class_reason="insufficient_kalshi_ladder_depth" if ladder_size < 3 else "line_not_in_current_ladder",
            line_ladder_rank=(unique_lines.index(line) + 1) if line in unique_lines else None,
            line_ladder_distance_from_central=None,
            line_ladder_size=ladder_size,
        )

    index = unique_lines.index(line)
    central_indexes = {(ladder_size - 1) // 2, ladder_size // 2}
    distance = min(abs(index - central_index) for central_index in central_indexes)
    if distance == 0:
        line_class = "central"
    elif distance == 1:
        line_class = "near_alternate"
    elif distance == 2:
        line_class = "deep_alternate"
    else:
        line_class = "tail"
    return LineClassification(
        line_class=line_class,
        line_class_reason="current_kalshi_ladder_position",
        line_ladder_rank=index + 1,
        line_ladder_distance_from_central=distance,
        line_ladder_size=ladder_size,
    )


def candidate_line_ladder_key(candidate: Any) -> tuple[object, ...] | None:
    family = str(getattr(candidate, "market_family", None) or getattr(candidate, "market_type", None) or "")
    if family not in LINE_FAMILIES:
        return None
    scope = str(getattr(candidate, "inning_scope", None) or _scope_for_family(family))
    game_id = getattr(candidate, "mlb_game_id", None)
    if family in SPREAD_FAMILIES:
        return (game_id, family, scope, _clean_team_code(getattr(candidate, "selection_code", None)) or "unknown")
    return (game_id, family, scope, "total")


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.0001"))
    except (InvalidOperation, ValueError):
        return None


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    normalized = value.quantize(Decimal("0.0001")).normalize()
    return format(normalized, "f")


def _signed_decimal_text(value: Decimal | None) -> str | None:
    text = _decimal_text(value)
    if text is None:
        return None
    return f"+{text}" if value is not None and value > 0 else text


def _scope_for_family(family: str | None) -> str | None:
    if family in {FIRST_FIVE_WINNER, FIRST_FIVE_SPREAD, FIRST_FIVE_TOTAL}:
        return "first_five"
    if family in {FULL_GAME_WINNER, FULL_GAME_SPREAD, FULL_GAME_TOTAL}:
        return "full_game"
    return None


def _scope_label(scope: str | None) -> str:
    return "FIRST FIVE" if scope == "first_five" else "FULL GAME"


def _clean_team_code(value: str | None) -> str | None:
    cleaned = "".join(character for character in str(value or "").upper() if character.isalnum())
    return cleaned or None


def _opponent_code(game: MlbGame | None, code: str | None) -> str | None:
    if game is None or not code:
        return None
    home = _clean_team_code(game.home_abbreviation)
    away = _clean_team_code(game.away_abbreviation)
    selected = _clean_team_code(code)
    if selected == home:
        return away
    if selected == away:
        return home
    return None


def _normalize_total_side(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"o", "over"}:
        return "over"
    if normalized in {"u", "under"}:
        return "under"
    return "unknown"


def _opposite_total_direction(direction: str) -> str:
    if direction == "over":
        return "under"
    if direction == "under":
        return "over"
    return "unknown"


def _spread_no_is_safe_complement(spread_verification: Any | None) -> bool:
    return bool(
        spread_verification is not None
        and getattr(spread_verification, "no_is_true_complement", False)
        and getattr(spread_verification, "complement_safe_for_paper_settlement", False)
    )


def _total_label(direction: str | None, line: Decimal | None, scope: str | None) -> str | None:
    if direction not in {"over", "under"}:
        return None
    line_text = _decimal_text(line)
    return f"{direction.upper()} {line_text} {_scope_label(scope)} TOTAL" if line_text else f"{direction.upper()} {_scope_label(scope)} TOTAL"


def _winner_label(team: str | None, direction: str | None, scope: str | None) -> str | None:
    if direction == "either_team_win":
        return "EITHER TEAM WINS FIRST FIVE"
    if not team:
        return None
    if direction == "not_win":
        return f"{team} DOES NOT WIN {_scope_label(scope)}"
    return f"{team} {_scope_label(scope)} WINNER"


def _spread_label(
    team: str | None,
    line: Decimal | None,
    direction: str | None,
    scope: str | None,
) -> str | None:
    if not team:
        return None
    line_text = _signed_decimal_text(line)
    if direction == "not_cover":
        return f"{team} DOES NOT COVER {line_text} {_scope_label(scope)} SPREAD" if line_text else f"{team} DOES NOT COVER {_scope_label(scope)} SPREAD"
    return f"{team} {line_text} {_scope_label(scope)} SPREAD" if line_text else f"{team} {_scope_label(scope)} SPREAD"


def _exposure_key(
    *,
    family: str | None,
    scope: str | None,
    direction: str | None,
    team: str | None,
    line: Decimal | None,
) -> str | None:
    if family is None:
        return None
    parts = [family, scope or "unknown_scope", direction or "unknown_direction"]
    if team:
        parts.append(team.lower())
    line_text = _decimal_text(line)
    if line_text is not None:
        parts.append(line_text.replace("-", "minus_").replace(".", "_"))
    return ":".join(parts)


def _concept_cluster_key(family: str | None, direction: str | None) -> str | None:
    if family is None or direction is None:
        return None
    return f"{family}_{direction}"


def _same_game_concept_cluster_key(
    *,
    game_id: int | None,
    concept_cluster_key: str | None,
    team: str | None,
) -> str | None:
    if game_id is None or concept_cluster_key is None:
        return None
    suffix = f":{team.lower()}" if team else ""
    return f"mlb_game:{game_id}:{concept_cluster_key}{suffix}"
