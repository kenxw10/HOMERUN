from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import re
from typing import Any

from app.models import KalshiMarket, MarketMapping, MlbGame
from app.services.contracts import FIRST_FIVE_SPREAD, FULL_GAME_SPREAD

SPREAD_FAMILIES = {FULL_GAME_SPREAD, FIRST_FIVE_SPREAD}
FULL_GAME_SPREAD_AUDIT_STATUSES = {
    "trusted_audit_only",
    "needs_review",
    "unsafe",
    "unsupported",
    "parse_error",
    "missing_market_data",
    "missing_game_mapping",
    "missing_line",
    "ambiguous_team_selection",
    "ambiguous_yes_no_semantics",
    "ambiguous_line_direction",
    "settlement_text_unverified",
    "push_behavior_uncertain",
}
SPREAD_TEXT_FIELDS = (
    "yes_sub_title",
    "yes_subtitle",
    "yes_title",
    "title",
    "subtitle",
    "rules_primary",
    "rules_secondary",
    "rules",
)
SPREAD_LINE_FIELDS = SPREAD_TEXT_FIELDS + ("custom_strike", "functional_strike", "strike")
NO_SPREAD_TEXT_FIELDS = ("no_sub_title", "no_subtitle", "no_title")
VERIFIED_STATUS = "verified"
UNVERIFIED_STATUS = "text_present_unverified"
CURRENT_AUDIT_METADATA_KEYS = {
    "audit_status",
    "reason_codes",
    "no_is_true_complement",
    "complement_safe_for_paper_settlement",
    "push_possible",
    "push_rule_verified",
}


@dataclass(frozen=True)
class SpreadVerification:
    family_key: str
    parser_status: str
    settlement_rule_status: str
    verified: bool
    selection_code: str | None
    line_value: Decimal | None
    inning_scope: str
    actual_contract_display: str | None
    no_contract_display: str | None
    normalized_no_equivalent_display: str | None
    parse_source: str | None
    raw_contract_text: dict[str, str | None]
    warnings: list[str]
    audit_status: str = "needs_review"
    reason_codes: list[str] | None = None
    yes_interpretation: str | None = None
    no_interpretation: str | None = None
    no_is_true_complement: bool = False
    complement_safe_for_paper_settlement: bool = False
    line_sign: str | None = None
    line_direction: str | None = None
    push_possible: bool = False
    push_condition: str | None = None
    push_rule_verified: bool = False

    def as_metadata(self) -> dict[str, object]:
        return {
            "family_key": self.family_key,
            "parser_status": self.parser_status,
            "settlement_rule_status": self.settlement_rule_status,
            "verified": self.verified,
            "paper_trade_allowed_if_enabled": self.verified,
            "selection_code": self.selection_code,
            "line_value": str(self.line_value) if self.line_value is not None else None,
            "inning_scope": self.inning_scope,
            "actual_contract_display": self.actual_contract_display,
            "no_contract_display": self.no_contract_display,
            "normalized_no_equivalent_display": self.normalized_no_equivalent_display,
            "parse_source": self.parse_source,
            "raw_contract_text": self.raw_contract_text,
            "warnings": list(self.warnings),
            "audit_status": self.audit_status,
            "reason_codes": list(self.reason_codes or []),
            "yes_interpretation": self.yes_interpretation,
            "no_interpretation": self.no_interpretation,
            "no_is_true_complement": self.no_is_true_complement,
            "complement_safe_for_paper_settlement": self.complement_safe_for_paper_settlement,
            "line_sign": self.line_sign,
            "line_direction": self.line_direction,
            "push_possible": self.push_possible,
            "push_condition": self.push_condition,
            "push_rule_verified": self.push_rule_verified,
        }


def _text_tokens(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", value.lower())


def _contains_phrase(tokens: list[str], phrase: str) -> bool:
    phrase_tokens = _text_tokens(phrase)
    return bool(phrase_tokens) and any(
        tokens[index : index + len(phrase_tokens)] == phrase_tokens for index in range(len(tokens))
    )


def _team_aliases(team: str | None, code: str | None) -> tuple[str, ...]:
    tokens = _text_tokens(team or "")
    aliases: list[str] = []
    if code:
        aliases.append(code)
    if tokens:
        aliases.append(" ".join(tokens))
    if len(tokens) >= 2:
        city = " ".join(tokens[:-1])
        nickname = tokens[-1]
        aliases.extend((city, nickname, f"{city} {nickname[0]}"))
    return tuple(dict.fromkeys(alias for alias in aliases if alias))


def _team_selection_from_text(value: object, game: MlbGame) -> str | None:
    tokens = _text_tokens(str(value or ""))
    if not tokens:
        return None
    matches = set()
    for team, code in (
        (game.home_team, game.home_abbreviation),
        (game.away_team, game.away_abbreviation),
    ):
        normalized_code = (code or "").upper()
        if normalized_code and any(_contains_phrase(tokens, alias) for alias in _team_aliases(team, normalized_code)):
            matches.add(normalized_code)
    if len(matches) == 1:
        return next(iter(matches))
    return None


def _line_from_text(value: object) -> Decimal | None:
    text = str(value or "")
    for match in re.finditer(r"(?<![A-Z0-9])([+-]\d+(?:\.\d+)?)(?![A-Z0-9])", text, flags=re.IGNORECASE):
        parsed = Decimal(match.group(1)).quantize(Decimal("0.0001"))
        if parsed != Decimal("0.0000"):
            return parsed
    return None


def _line_text(value: Decimal | None) -> str:
    if value is None:
        return ""
    normalized = value.normalize()
    return f"{normalized:f}".rstrip("0").rstrip(".")


def _line_text_variants(value: Decimal | None) -> set[str]:
    if value is None:
        return set()
    variants = {_format_line(value)}
    if value == Decimal("0"):
        variants.add(_line_text(value))
    return {variant for variant in variants if variant}


def _text_mentions_line(value: object, line_value: Decimal | None) -> bool:
    text = str(value or "").replace("−", "-")
    return any(
        re.search(rf"(?<![A-Z0-9.+-]){re.escape(variant)}(?![A-Z0-9.])", text, flags=re.IGNORECASE)
        for variant in _line_text_variants(line_value)
    )


def _format_line(value: Decimal | None) -> str:
    if value is None:
        return ""
    numeric = float(value)
    if numeric > 0:
        return f"+{numeric:g}"
    return f"{numeric:g}"


def _line_sign(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value > 0:
        return "positive"
    if value < 0:
        return "negative"
    return "zero"


def _line_direction(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value > 0:
        return "selected_team_gets_runs"
    if value < 0:
        return "selected_team_lays_runs"
    return "pickem"


def _push_possible(value: Decimal | None) -> bool:
    if value is None:
        return False
    return value == value.to_integral_value()


def _rules_verify_push(value: Decimal | None, rules_text: str | None) -> bool:
    if not _push_possible(value):
        return True
    tokens = set(_text_tokens(rules_text or ""))
    return bool(tokens & {"push", "void", "tie", "ties", "refund", "refunded", "cancel", "canceled"})


def _team_display(game: MlbGame, code: str | None) -> str | None:
    normalized = (code or "").upper()
    if normalized == (game.home_abbreviation or "").upper():
        return game.home_team or game.home_abbreviation
    if normalized == (game.away_abbreviation or "").upper():
        return game.away_team or game.away_abbreviation
    return code


def _opponent_code(game: MlbGame, code: str | None) -> str | None:
    normalized = (code or "").upper()
    home = (game.home_abbreviation or "").upper()
    away = (game.away_abbreviation or "").upper()
    if normalized == home:
        return away or None
    if normalized == away:
        return home or None
    return None


def _extract_raw(raw: dict[str, Any] | None, market: KalshiMarket | None = None) -> dict[str, Any]:
    payload = dict(raw or {})
    if market is not None:
        payload.setdefault("ticker", market.ticker)
        payload.setdefault("title", market.title)
        payload.setdefault("subtitle", market.subtitle)
        payload.setdefault("rules", market.rules)
        payload.setdefault("yes_subtitle", market.yes_subtitle)
        payload.setdefault("no_subtitle", market.no_subtitle)
        if isinstance(market.raw_payload, dict):
            for key, value in market.raw_payload.items():
                payload.setdefault(key, value)
    return payload


def _reason_status(reason_codes: list[str]) -> str:
    priority = (
        ("unsupported", "unsupported_family"),
        ("missing_market_data", "missing_market_data"),
        ("missing_game_mapping", "missing_game_mapping"),
        ("missing_line", "missing_line"),
        ("ambiguous_team_selection", "team_selection_not_verified"),
        ("ambiguous_yes_no_semantics", "yes_contract_text_missing"),
        ("ambiguous_yes_no_semantics", "no_contract_text_missing"),
        ("ambiguous_yes_no_semantics", "no_contract_text_conflicts_with_expected_complement"),
        ("ambiguous_line_direction", "line_direction_not_verified_from_text"),
        ("push_behavior_uncertain", "push_behavior_unverified"),
        ("settlement_text_unverified", "settlement_text_missing"),
    )
    for status, reason in priority:
        if reason in reason_codes:
            return status
    return "trusted_audit_only" if not reason_codes else "needs_review"


def _first_text(payload: dict[str, Any], fields: tuple[str, ...]) -> tuple[str | None, str | None]:
    for field in fields:
        value = payload.get(field)
        if value:
            return str(value), field
    return None, None


def _scope(family_key: str) -> str:
    return "first_five" if family_key == FIRST_FIVE_SPREAD else "full_game"


def _scope_display(scope: str) -> str:
    return "first 5 innings" if scope == "first_five" else "full game"


def verify_spread_market(
    *,
    game: MlbGame,
    family_key: str,
    raw: dict[str, Any] | None = None,
    market: KalshiMarket | None = None,
    line_value: Decimal | None = None,
    selection_code: str | None = None,
) -> SpreadVerification:
    payload = _extract_raw(raw, market)
    scope = _scope(family_key)
    yes_text, yes_source = _first_text(payload, SPREAD_TEXT_FIELDS)
    no_text, _no_source = _first_text(payload, NO_SPREAD_TEXT_FIELDS)
    warnings: list[str] = []

    parsed_selection = (selection_code or "").upper() or None
    parsed_line = line_value.quantize(Decimal("0.0001")) if line_value is not None else None
    selection_source = None
    line_source = None
    for field in SPREAD_TEXT_FIELDS:
        value = payload.get(field)
        selection = _team_selection_from_text(value, game)
        if selection:
            parsed_selection = selection
            selection_source = field
            break
    for field in SPREAD_LINE_FIELDS:
        value = payload.get(field)
        line = _line_from_text(value)
        if line is not None:
            parsed_line = line
            line_source = field
            break

    parse_source = (
        f"{selection_source}+{line_source}"
        if selection_source is not None and line_source is not None
        else selection_source or line_source
    )

    team = _team_display(game, parsed_selection)
    opponent = _team_display(game, _opponent_code(game, parsed_selection))
    home_code = (game.home_abbreviation or "").upper()
    away_code = (game.away_abbreviation or "").upper()
    team_codes = {code for code in (home_code, away_code) if code}
    rules_text = str(payload.get("rules") or payload.get("rules_primary") or payload.get("rules_secondary") or "") or None
    expected_no_equivalent_line = -parsed_line if parsed_line is not None else None
    no_text_mentions_expected = bool(
        no_text
        and opponent
        and _team_selection_from_text(no_text, game) == _opponent_code(game, parsed_selection)
        and _text_mentions_line(no_text, expected_no_equivalent_line)
    )

    reason_codes: list[str] = []
    if family_key not in SPREAD_FAMILIES:
        reason_codes.append("unsupported_family")
    if not payload.get("ticker") and market is None:
        reason_codes.append("missing_market_data")
    if parsed_line is None:
        reason_codes.append("missing_line")
    if parsed_selection not in team_codes or selection_source is None:
        reason_codes.append("team_selection_not_verified")
        warnings.append("SPREAD_SELECTION_NOT_VERIFIED_FROM_KALSHI_TEXT")
    if not yes_text:
        reason_codes.append("yes_contract_text_missing")
        warnings.append("SPREAD_YES_TEXT_MISSING")
    if parsed_line is not None and line_source is None:
        reason_codes.append("line_direction_not_verified_from_text")
        warnings.append("SPREAD_LINE_NOT_VERIFIED_FROM_KALSHI_TEXT")
    if no_text is None:
        reason_codes.append("no_contract_text_missing")
    elif not no_text_mentions_expected:
        reason_codes.append("no_contract_text_conflicts_with_expected_complement")
    if rules_text is None:
        reason_codes.append("settlement_text_missing")
    if _push_possible(parsed_line) and not _rules_verify_push(parsed_line, rules_text):
        reason_codes.append("push_behavior_unverified")

    audit_status = _reason_status(reason_codes)
    verified = audit_status == "trusted_audit_only"

    actual_display = None
    no_display = None
    no_equivalent = None
    yes_interpretation = None
    no_interpretation = None
    push_condition = None
    if parsed_selection and parsed_line is not None:
        scope_label = _scope_display(scope)
        actual_display = f"YES on {team or parsed_selection} {_format_line(parsed_line)} {scope_label}".upper()
        no_display = f"NO on {team or parsed_selection} {_format_line(parsed_line)} {scope_label}".upper()
        yes_interpretation = f"{team or parsed_selection} {_format_line(parsed_line)} covers {scope_label}"
        no_interpretation = f"{team or parsed_selection} {_format_line(parsed_line)} does not cover {scope_label}"
        push_condition = f"selected_team_margin + {_format_line(parsed_line)} equals 0"
        if opponent:
            inverse_line = -parsed_line
            no_equivalent = f"{opponent} {_format_line(inverse_line)} {scope_label} equivalent".upper()

    return SpreadVerification(
        family_key=family_key,
        parser_status=VERIFIED_STATUS if verified else UNVERIFIED_STATUS,
        settlement_rule_status=VERIFIED_STATUS if verified else UNVERIFIED_STATUS,
        verified=verified,
        selection_code=parsed_selection,
        line_value=parsed_line,
        inning_scope=scope,
        actual_contract_display=actual_display,
        no_contract_display=no_display,
        normalized_no_equivalent_display=no_equivalent,
        parse_source=parse_source,
        raw_contract_text={
            "yes": yes_text,
            "no": no_text,
            "rules": rules_text,
        },
        warnings=warnings,
        audit_status=audit_status,
        reason_codes=reason_codes,
        yes_interpretation=yes_interpretation.upper() if yes_interpretation else None,
        no_interpretation=no_interpretation.upper() if no_interpretation else None,
        no_is_true_complement=parsed_selection in team_codes and parsed_line is not None and bool(no_text_mentions_expected),
        complement_safe_for_paper_settlement=verified,
        line_sign=_line_sign(parsed_line),
        line_direction=_line_direction(parsed_line),
        push_possible=_push_possible(parsed_line),
        push_condition=push_condition,
        push_rule_verified=_rules_verify_push(parsed_line, rules_text),
    )


def spread_verification_from_mapping(
    *,
    game: MlbGame,
    mapping: MarketMapping,
    market: KalshiMarket,
) -> SpreadVerification:
    family_key = mapping.market_family or market.market_family or market.market_type or ""
    metadata = mapping.mapping_metadata or {}
    existing = metadata.get("spread_verification")
    if (
        isinstance(existing, dict)
        and existing.get("verified") is True
        and CURRENT_AUDIT_METADATA_KEYS.issubset(existing.keys())
        and existing.get("audit_status") in FULL_GAME_SPREAD_AUDIT_STATUSES
    ):
        line = existing.get("line_value")
        return SpreadVerification(
            family_key=family_key,
            parser_status=str(existing.get("parser_status") or VERIFIED_STATUS),
            settlement_rule_status=str(existing.get("settlement_rule_status") or VERIFIED_STATUS),
            verified=True,
            selection_code=str(existing.get("selection_code") or mapping.selection_code or market.selection_code or ""),
            line_value=Decimal(str(line)).quantize(Decimal("0.0001")) if line is not None else mapping.line_value or market.line_value,
            inning_scope=str(existing.get("inning_scope") or mapping.inning_scope or market.inning_scope or _scope(family_key)),
            actual_contract_display=existing.get("actual_contract_display") if isinstance(existing.get("actual_contract_display"), str) else None,
            no_contract_display=existing.get("no_contract_display") if isinstance(existing.get("no_contract_display"), str) else None,
            normalized_no_equivalent_display=(
                existing.get("normalized_no_equivalent_display")
                if isinstance(existing.get("normalized_no_equivalent_display"), str)
                else None
            ),
            parse_source=str(existing.get("parse_source") or "metadata"),
            raw_contract_text=existing.get("raw_contract_text") if isinstance(existing.get("raw_contract_text"), dict) else {},
            warnings=list(existing.get("warnings") or []) if isinstance(existing.get("warnings"), list) else [],
            audit_status=str(existing.get("audit_status") or "trusted_audit_only"),
            reason_codes=list(existing.get("reason_codes") or []) if isinstance(existing.get("reason_codes"), list) else [],
            yes_interpretation=existing.get("yes_interpretation") if isinstance(existing.get("yes_interpretation"), str) else None,
            no_interpretation=existing.get("no_interpretation") if isinstance(existing.get("no_interpretation"), str) else None,
            no_is_true_complement=bool(existing.get("no_is_true_complement", True)),
            complement_safe_for_paper_settlement=bool(existing.get("complement_safe_for_paper_settlement", True)),
            line_sign=existing.get("line_sign") if isinstance(existing.get("line_sign"), str) else None,
            line_direction=existing.get("line_direction") if isinstance(existing.get("line_direction"), str) else None,
            push_possible=bool(existing.get("push_possible", False)),
            push_condition=existing.get("push_condition") if isinstance(existing.get("push_condition"), str) else None,
            push_rule_verified=bool(existing.get("push_rule_verified", False)),
        )
    return verify_spread_market(
        game=game,
        family_key=family_key,
        market=market,
        line_value=mapping.line_value or market.line_value,
        selection_code=mapping.selection_code or market.selection_code,
    )
