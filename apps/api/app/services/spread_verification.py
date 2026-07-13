from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
import re
from typing import Any

from app.models import KalshiMarket, MarketMapping, MlbGame
from app.services.contracts import FIRST_FIVE_SPREAD, FULL_GAME_SPREAD

SPREAD_FAMILIES = {FULL_GAME_SPREAD, FIRST_FIVE_SPREAD}
FULL_GAME_SPREAD_TICKER_PREFIX = "KXMLBSPREAD-"
FIRST_FIVE_SPREAD_TICKER_PREFIX = "KXMLBF5SPREAD-"
SPREAD_TICKER_PREFIX_BY_FAMILY = {
    FULL_GAME_SPREAD: FULL_GAME_SPREAD_TICKER_PREFIX,
    FIRST_FIVE_SPREAD: FIRST_FIVE_SPREAD_TICKER_PREFIX,
}
SPREAD_AUDIT_STATUSES = {
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
FULL_GAME_SPREAD_AUDIT_STATUSES = SPREAD_AUDIT_STATUSES
FIRST_FIVE_SPREAD_AUDIT_STATUSES = SPREAD_AUDIT_STATUSES
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
SPREAD_RULE_TEXT_FIELDS = ("rules_primary", "rules_secondary", "rules")
SPREAD_YES_TEXT_FIELDS = ("yes_sub_title", "yes_subtitle", "yes_title")
NO_SPREAD_TEXT_FIELDS = ("no_sub_title", "no_subtitle", "no_title")
VERIFIED_STATUS = "verified"
UNVERIFIED_STATUS = "text_present_unverified"
TRUST_EVIDENCE_REASON_CODES = {
    "rules_text_spread_condition_verified",
    "selected_team_verified",
    "selected_team_threshold_verified",
    "binary_yes_no_complement_verified",
    "half_run_no_push_verified",
    "settlement_formula_verified",
    "first_five_scope_verified",
    "first_five_official_result_source_verified",
}
CURRENT_AUDIT_METADATA_KEYS = {
    "audit_status",
    "reason_codes",
    "no_is_true_complement",
    "complement_safe_for_paper_settlement",
    "push_possible",
    "push_rule_verified",
    "condition_type",
    "threshold_runs",
    "settlement_formula",
    "no_text_source",
    "no_complement_source",
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
    condition_type: str | None = None
    threshold_runs: Decimal | None = None
    raw_threshold_runs: Decimal | None = None
    selected_team_margin_required_gt: Decimal | None = None
    display_spread_line: Decimal | None = None
    settlement_formula: str | None = None
    no_text_source: str | None = None
    no_complement_source: str | None = None
    no_complement_confidence: str | None = None
    ticker_suffix_line_raw: str | None = None

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
            "condition_type": self.condition_type,
            "threshold_runs": str(self.threshold_runs) if self.threshold_runs is not None else None,
            "raw_threshold_runs": str(self.raw_threshold_runs) if self.raw_threshold_runs is not None else None,
            "selected_team_margin_required_gt": (
                str(self.selected_team_margin_required_gt)
                if self.selected_team_margin_required_gt is not None
                else None
            ),
            "display_spread_line": str(self.display_spread_line) if self.display_spread_line is not None else None,
            "settlement_formula": self.settlement_formula,
            "no_text_source": self.no_text_source,
            "no_complement_source": self.no_complement_source,
            "no_complement_confidence": self.no_complement_confidence,
            "ticker_suffix_line_raw": self.ticker_suffix_line_raw,
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


@dataclass(frozen=True)
class RulesSpreadCondition:
    condition_type: str
    selection_code: str
    threshold_runs: Decimal
    raw_threshold_runs: Decimal
    normalized_spread_line: Decimal
    team_text: str
    source: str


def _parse_rules_spread_condition(
    rules_text: str | None,
    game: MlbGame,
    source: str | None,
) -> RulesSpreadCondition | None:
    if not rules_text:
        return None
    text = str(rules_text).replace("−", "-")
    yes_result_pattern = r"(?:resolves?|settles?)\s+to\s+yes|yes\s+wins?"
    more_than_pattern = re.compile(
        r"\bif\s+(?:the\s+)?(?P<team>.+?)\s+wins?\s+by\s+more\s+than\s+"
        rf"(?P<threshold>\d+(?:\.\d+)?)\s+runs?\b.*?\b(?:{yes_result_pattern})\b",
        flags=re.IGNORECASE | re.DOTALL,
    )
    or_more_pattern = re.compile(
        r"\bif\s+(?:the\s+)?(?P<team>.+?)\s+wins?\s+by\s+"
        rf"(?P<threshold>\d+(?:\.\d+)?)\s+or\s+more\s+runs?\b.*?\b(?:{yes_result_pattern})\b",
        flags=re.IGNORECASE | re.DOTALL,
    )
    match = more_than_pattern.search(text)
    raw_threshold = None
    normalized_threshold = None
    if match is not None:
        raw_threshold = Decimal(match.group("threshold")).quantize(Decimal("0.0001"))
        normalized_threshold = raw_threshold
    else:
        match = or_more_pattern.search(text)
        if match is not None:
            raw_threshold = Decimal(match.group("threshold")).quantize(Decimal("0.0001"))
            normalized_threshold = (raw_threshold - Decimal("0.5")).quantize(Decimal("0.0001"))
    if match is None:
        return None
    if raw_threshold is None or normalized_threshold is None or normalized_threshold <= 0:
        return None
    team_text = re.sub(r"\s+", " ", match.group("team")).strip(" ,.;:-")
    selection = _team_selection_from_text(team_text, game)
    if selection is None:
        return None
    return RulesSpreadCondition(
        condition_type="team_wins_by_more_than",
        selection_code=selection,
        threshold_runs=normalized_threshold,
        raw_threshold_runs=raw_threshold,
        normalized_spread_line=(-normalized_threshold).quantize(Decimal("0.0001")),
        team_text=team_text,
        source=source or "rules_text",
    )


def _line_text(value: Decimal | None) -> str:
    if value is None:
        return ""
    text = f"{value.normalize():f}"
    if "." not in text:
        return text
    return text.rstrip("0").rstrip(".")


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


def _is_half_run_line(value: Decimal | None) -> bool:
    if value is None:
        return False
    return abs(value % Decimal("1")) == Decimal("0.5000")


def _rules_verify_push(value: Decimal | None, rules_text: str | None) -> bool:
    if value is None:
        return False
    if _is_half_run_line(value):
        return True
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


def _ticker_suffix_line_raw(ticker: str | None, selection_code: str | None) -> str | None:
    if not ticker:
        return None
    suffix = ticker.upper().rsplit("-", 1)[-1]
    selection = (selection_code or "").upper()
    if selection and suffix.startswith(selection):
        raw = suffix[len(selection) :]
        return raw or None
    compact = re.match(r"^[A-Z]{2,5}([+-]?\d+(?:\.\d+)?)$", suffix)
    if compact:
        return compact.group(1)
    if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", suffix):
        return suffix
    return None


def _normal_text(value: str | None) -> str:
    return " ".join(_text_tokens(value or ""))


def _no_text_source(no_text: str | None, yes_text: str | None, rules_text: str | None) -> str:
    if no_text is None:
        return "missing"
    normalized_no = _normal_text(no_text)
    if normalized_no and normalized_no in {_normal_text(yes_text), _normal_text(rules_text)}:
        return "duplicated_yes_text"
    return "explicit_no_text"


def _binary_market_confirmed(payload: dict[str, Any], market: KalshiMarket | None, family_key: str) -> bool:
    expected_prefix = SPREAD_TICKER_PREFIX_BY_FAMILY.get(family_key)
    if expected_prefix is None:
        return False
    ticker = str(payload.get("ticker") or (market.ticker if market is not None else "") or "").upper()
    return bool(ticker) and ticker.startswith(expected_prefix)


def recognized_spread_ticker_family(ticker: str | None) -> str | None:
    normalized = str(ticker or "").upper()
    for family_key, prefix in SPREAD_TICKER_PREFIX_BY_FAMILY.items():
        if normalized.startswith(prefix):
            return family_key
    return None


def _first_five_scope_evidence(payload: dict[str, Any], market: KalshiMarket | None = None) -> bool:
    ticker = str(payload.get("ticker") or (market.ticker if market is not None else "") or "").upper()
    if not ticker.startswith(FIRST_FIVE_SPREAD_TICKER_PREFIX):
        return False
    text = " ".join(
        str(payload.get(field) or "")
        for field in (
            "title",
            "subtitle",
            "yes_title",
            "yes_sub_title",
            "yes_subtitle",
            "no_title",
            "no_sub_title",
            "no_subtitle",
            "rules_primary",
            "rules_secondary",
            "rules",
        )
    )
    tokens = _text_tokens(text)
    return _contains_phrase(tokens, "first 5 innings") or _contains_phrase(tokens, "first five innings")


def _first_five_official_result_source_evidence(payload: dict[str, Any]) -> bool:
    rules_text = " ".join(str(payload.get(field) or "") for field in SPREAD_RULE_TEXT_FIELDS)
    tokens = _text_tokens(rules_text)
    first_five_rules = _contains_phrase(tokens, "first 5 innings") or _contains_phrase(tokens, "first five innings")
    result_language = bool(set(tokens) & {"score", "scores", "runs", "result", "resolves", "settles", "settlement"})
    return first_five_rules and result_language


def _supporting_spread_conflicts(
    *,
    payload: dict[str, Any],
    game: MlbGame,
    selection_code: str | None,
    line_value: Decimal | None,
) -> list[str]:
    conflicts: list[str] = []
    selected = (selection_code or "").upper()
    for field in ("title", "yes_sub_title", "yes_subtitle", "yes_title", "subtitle"):
        value = payload.get(field)
        if not value:
            continue
        supporting_selection = _team_selection_from_text(value, game)
        if supporting_selection and selected and supporting_selection != selected:
            conflicts.append("title_rules_team_conflict")
        supporting_line = _line_from_text(value)
        if supporting_line is not None and line_value is not None and supporting_line != line_value:
            conflicts.append("subtitle_rules_line_conflict")
    return list(dict.fromkeys(conflicts))


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
        ("parse_error", "first_five_contract_parse_error"),
        ("missing_line", "missing_line"),
        ("parse_error", "rules_text_unparseable"),
        ("unsafe", "spread_family_ticker_conflict"),
        ("unsafe", "spread_family_metadata_conflict"),
        ("unsafe", "explicit_no_text_conflicts_with_binary_complement"),
        ("unsafe", "title_rules_team_conflict"),
        ("ambiguous_team_selection", "team_selection_not_verified"),
        ("ambiguous_team_selection", "selected_team_not_verified_from_rules_text"),
        ("ambiguous_yes_no_semantics", "yes_contract_text_missing"),
        ("ambiguous_yes_no_semantics", "no_contract_text_missing"),
        ("ambiguous_yes_no_semantics", "no_contract_text_conflicts_with_expected_complement"),
        ("ambiguous_yes_no_semantics", "binary_complement_unverified"),
        ("ambiguous_line_direction", "line_direction_not_verified_from_text"),
        ("ambiguous_line_direction", "subtitle_rules_line_conflict"),
        ("push_behavior_uncertain", "integer_push_rule_unverified"),
        ("push_behavior_uncertain", "push_behavior_unverified"),
        ("settlement_text_unverified", "settlement_text_missing"),
    )
    for status, reason in priority:
        if reason in reason_codes:
            return status
    blocking_reasons = [reason for reason in reason_codes if reason not in TRUST_EVIDENCE_REASON_CODES]
    return "trusted_audit_only" if not blocking_reasons else "needs_review"


def _first_text(payload: dict[str, Any], fields: tuple[str, ...]) -> tuple[str | None, str | None]:
    for field in fields:
        value = payload.get(field)
        if value:
            return str(value), field
    return None, None


def _all_text(payload: dict[str, Any], fields: tuple[str, ...]) -> list[tuple[str, str]]:
    return [(field, str(payload[field])) for field in fields if payload.get(field)]


def _combined_text(entries: list[tuple[str, str]]) -> str | None:
    if not entries:
        return None
    return "\n".join(text for _field, text in entries)


def _scope(family_key: str) -> str:
    return "first_five" if family_key == FIRST_FIVE_SPREAD else "full_game"


def enforce_spread_family_consistency(
    verification: SpreadVerification,
    *,
    expected_family: str,
    ticker: str | None,
) -> SpreadVerification:
    """Prevent a recognized canonical ticker from being trusted for another family."""
    if expected_family not in SPREAD_FAMILIES:
        return verification

    reason_codes = list(verification.reason_codes or [])
    recognized_family = recognized_spread_ticker_family(ticker)
    conflicts: list[str] = []
    if recognized_family is not None and recognized_family != expected_family:
        conflicts.append("spread_family_ticker_conflict")
    if verification.family_key != expected_family or verification.inning_scope != _scope(expected_family):
        conflicts.append("spread_family_metadata_conflict")
    if not conflicts:
        return verification

    for reason in conflicts:
        if reason not in reason_codes:
            reason_codes.append(reason)
    return replace(
        verification,
        family_key=expected_family,
        parser_status=UNVERIFIED_STATUS,
        settlement_rule_status=UNVERIFIED_STATUS,
        verified=False,
        audit_status="unsafe",
        reason_codes=reason_codes,
        complement_safe_for_paper_settlement=False,
    )


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
    yes_text, _yes_source = _first_text(payload, SPREAD_YES_TEXT_FIELDS)
    if yes_text is None:
        yes_text, _yes_source = _first_text(payload, ("title", "subtitle"))
    no_text, _no_source = _first_text(payload, NO_SPREAD_TEXT_FIELDS)
    rule_entries = _all_text(payload, SPREAD_RULE_TEXT_FIELDS)
    rules_text = _combined_text(rule_entries)
    rules_condition = None
    if family_key in SPREAD_FAMILIES:
        for rules_source, candidate_rules_text in rule_entries:
            rules_condition = _parse_rules_spread_condition(candidate_rules_text, game, rules_source)
            if rules_condition is not None:
                break
    warnings: list[str] = []

    parsed_selection = (selection_code or "").upper() or None
    parsed_line = line_value.quantize(Decimal("0.0001")) if line_value is not None else None
    selection_source = None
    line_source = None
    condition_type = None
    threshold_runs = None
    if rules_condition is not None:
        parsed_selection = rules_condition.selection_code
        parsed_line = rules_condition.normalized_spread_line
        selection_source = "rules_text"
        line_source = "rules_text"
        condition_type = rules_condition.condition_type
        threshold_runs = rules_condition.threshold_runs
    else:
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
        threshold_runs = (-parsed_line).quantize(Decimal("0.0001")) if parsed_line is not None else None

    parse_source = "rules_text" if rules_condition is not None else (
        f"{selection_source}+{line_source}"
        if selection_source is not None and line_source is not None
        else selection_source or line_source
    )

    team = _team_display(game, parsed_selection)
    opponent = _team_display(game, _opponent_code(game, parsed_selection))
    home_code = (game.home_abbreviation or "").upper()
    away_code = (game.away_abbreviation or "").upper()
    team_codes = {code for code in (home_code, away_code) if code}
    expected_no_equivalent_line = -parsed_line if parsed_line is not None else None
    no_text_kind = _no_text_source(no_text, yes_text, rules_text)
    explicit_no_text_mentions_expected = bool(
        no_text
        and no_text_kind == "explicit_no_text"
        and opponent
        and _team_selection_from_text(no_text, game) == _opponent_code(game, parsed_selection)
        and _text_mentions_line(no_text, expected_no_equivalent_line)
    )
    no_explicit_conflict = no_text_kind == "explicit_no_text" and not explicit_no_text_mentions_expected
    yes_rule_verified = rules_condition is not None and parsed_selection in team_codes and parsed_line is not None
    binary_confirmed = _binary_market_confirmed(payload, market, family_key)
    no_complement_source = None
    no_complement_confidence = None
    if yes_rule_verified and binary_confirmed and (
        family_key == FULL_GAME_SPREAD or no_text is None
    ) and not no_explicit_conflict:
        no_complement_source = "binary_market_complement"
        no_complement_confidence = "high"
    elif explicit_no_text_mentions_expected:
        no_complement_source = "explicit_no_text"
        no_complement_confidence = "medium"
    threshold_required_gt = (-parsed_line).quantize(Decimal("0.0001")) if parsed_line is not None else None
    if threshold_runs is None:
        threshold_runs = threshold_required_gt
    settlement_formula = (
        f"selected_team_runs - opponent_runs > {_line_text(threshold_required_gt)}"
        if threshold_required_gt is not None
        else None
    )

    reason_codes: list[str] = []
    if family_key not in SPREAD_FAMILIES:
        reason_codes.append("unsupported_family")
    ticker_family = recognized_spread_ticker_family(str(payload.get("ticker") or ""))
    if family_key in SPREAD_FAMILIES and ticker_family is not None and ticker_family != family_key:
        reason_codes.append("spread_family_ticker_conflict")
    if not payload.get("ticker") and market is None:
        reason_codes.append("missing_market_data")
    if family_key == FULL_GAME_SPREAD and rules_text and rules_condition is None:
        reason_codes.append("rules_text_unparseable")
    if parsed_line is None:
        reason_codes.append("missing_line")
    if parsed_selection not in team_codes or selection_source is None:
        reason_codes.append("team_selection_not_verified")
        warnings.append("SPREAD_SELECTION_NOT_VERIFIED_FROM_KALSHI_TEXT")
    elif family_key == FULL_GAME_SPREAD and selection_source != "rules_text" and rules_text is not None:
        reason_codes.append("selected_team_not_verified_from_rules_text")
    if not yes_text and rules_condition is None:
        reason_codes.append("yes_contract_text_missing")
        warnings.append("SPREAD_YES_TEXT_MISSING")
    if (
        family_key == FULL_GAME_SPREAD
        and parsed_line is not None
        and line_source != "rules_text"
        and rules_text is not None
    ):
        reason_codes.append("line_direction_not_verified_from_text")
        warnings.append("SPREAD_LINE_NOT_VERIFIED_FROM_KALSHI_TEXT")
    elif parsed_line is not None and line_source is None:
        reason_codes.append("line_direction_not_verified_from_text")
        warnings.append("SPREAD_LINE_NOT_VERIFIED_FROM_KALSHI_TEXT")
    if family_key in SPREAD_FAMILIES and rules_condition is not None:
        reason_codes.extend(
            _supporting_spread_conflicts(
                payload=payload,
                game=game,
                selection_code=parsed_selection,
                line_value=parsed_line,
            )
        )
    if no_explicit_conflict:
        reason_codes.append("explicit_no_text_conflicts_with_binary_complement")
        reason_codes.append("no_contract_text_conflicts_with_expected_complement")
    elif family_key == FULL_GAME_SPREAD:
        if not no_complement_source:
            reason_codes.append("binary_complement_unverified")
    else:
        if not no_complement_source and no_text is None:
            reason_codes.append("no_contract_text_missing")
        elif not no_complement_source and not explicit_no_text_mentions_expected:
            reason_codes.append("no_contract_text_conflicts_with_expected_complement")
    if rules_text is None:
        reason_codes.append("settlement_text_missing")
    if _push_possible(parsed_line) and not _rules_verify_push(parsed_line, rules_text):
        reason_codes.append("integer_push_rule_unverified")
        reason_codes.append("push_behavior_unverified")
    if rules_condition is not None:
        reason_codes.append("rules_text_spread_condition_verified")
        reason_codes.append("selected_team_threshold_verified")
    if parsed_selection in team_codes and selection_source is not None:
        reason_codes.append("selected_team_verified")
    if no_complement_source:
        reason_codes.append("binary_yes_no_complement_verified")
    if _is_half_run_line(parsed_line):
        reason_codes.append("half_run_no_push_verified")
    if settlement_formula:
        reason_codes.append("settlement_formula_verified")
    if family_key == FIRST_FIVE_SPREAD:
        if _first_five_scope_evidence(payload, market):
            reason_codes.append("first_five_scope_verified")
        else:
            reason_codes.append("first_five_scope_unverified")
        if _first_five_official_result_source_evidence(payload):
            reason_codes.append("first_five_official_result_source_verified")
        else:
            reason_codes.append("first_five_official_result_source_unverified")
        if parsed_line is None or line_source is None or parsed_selection not in team_codes or selection_source is None:
            reason_codes.append("first_five_contract_parse_error")

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
        push_condition = (
            "not_applicable_half_run_line"
            if _is_half_run_line(parsed_line)
            else f"selected_team_margin + {_format_line(parsed_line)} equals 0"
        )
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
        no_is_true_complement=bool(no_complement_source),
        complement_safe_for_paper_settlement=verified,
        line_sign=_line_sign(parsed_line),
        line_direction=_line_direction(parsed_line),
        push_possible=_push_possible(parsed_line),
        push_condition=push_condition,
        push_rule_verified=_rules_verify_push(parsed_line, rules_text),
        condition_type=condition_type,
        threshold_runs=threshold_runs,
        raw_threshold_runs=rules_condition.raw_threshold_runs if rules_condition is not None else None,
        selected_team_margin_required_gt=threshold_required_gt,
        display_spread_line=parsed_line,
        settlement_formula=settlement_formula,
        no_text_source=no_text_kind,
        no_complement_source=no_complement_source,
        no_complement_confidence=no_complement_confidence,
        ticker_suffix_line_raw=_ticker_suffix_line_raw(str(payload.get("ticker") or ""), parsed_selection),
    )


def spread_verification_from_mapping(
    *,
    game: MlbGame,
    mapping: MarketMapping,
    market: KalshiMarket,
) -> SpreadVerification:
    family_key = mapping.market_family or market.market_family or market.market_type or ""
    cached = spread_verification_from_cached_metadata(mapping=mapping, market=market)
    if cached is not None and cached.verified:
        return cached
    return enforce_spread_family_consistency(
        verify_spread_market(
            game=game,
            family_key=family_key,
            market=market,
            line_value=mapping.line_value or market.line_value,
            selection_code=mapping.selection_code or market.selection_code,
        ),
        expected_family=family_key,
        ticker=market.ticker,
    )


def spread_verification_from_cached_metadata(
    *,
    mapping: MarketMapping,
    market: KalshiMarket | None = None,
) -> SpreadVerification | None:
    family_key = mapping.market_family or (market.market_family if market is not None else None) or (
        market.market_type if market is not None else None
    ) or ""
    metadata = mapping.mapping_metadata or {}
    existing = metadata.get("spread_verification")
    if not (
        isinstance(existing, dict)
        and CURRENT_AUDIT_METADATA_KEYS.issubset(existing.keys())
        and existing.get("audit_status") in SPREAD_AUDIT_STATUSES
    ):
        return None
    if existing.get("family_key") not in {None, family_key}:
        return None
    expected_scope = _scope(family_key)
    if existing.get("inning_scope") not in {None, expected_scope}:
        return None
    line = existing.get("line_value")
    market_line = market.line_value if market is not None else None
    market_selection = market.selection_code if market is not None else None
    market_scope = market.inning_scope if market is not None else None
    raw_contract_text = existing.get("raw_contract_text") if isinstance(existing.get("raw_contract_text"), dict) else {}
    reason_codes = list(existing.get("reason_codes") or []) if isinstance(existing.get("reason_codes"), list) else []
    audit_status = str(existing.get("audit_status") or "trusted_audit_only")
    verified = bool(existing.get("verified"))
    if family_key == FIRST_FIVE_SPREAD:
        cached_payload = {
            "ticker": market.ticker if market is not None else None,
            "yes_subtitle": raw_contract_text.get("yes"),
            "no_subtitle": raw_contract_text.get("no"),
            "rules": raw_contract_text.get("rules"),
        }
        if not _first_five_scope_evidence(cached_payload, market):
            reason_codes = [code for code in reason_codes if code != "first_five_scope_verified"]
            reason_codes.append("first_five_scope_unverified")
            verified = False
        if not _first_five_official_result_source_evidence(cached_payload):
            reason_codes = [
                code for code in reason_codes if code != "first_five_official_result_source_verified"
            ]
            reason_codes.append("first_five_official_result_source_unverified")
            verified = False
        if "first_five_scope_unverified" in reason_codes or "first_five_official_result_source_unverified" in reason_codes:
            audit_status = "needs_review"
    verification = SpreadVerification(
        family_key=family_key,
        parser_status=str(existing.get("parser_status") or VERIFIED_STATUS),
        settlement_rule_status=str(existing.get("settlement_rule_status") or VERIFIED_STATUS),
        verified=verified,
        selection_code=str(existing.get("selection_code") or mapping.selection_code or market_selection or "") or None,
        line_value=Decimal(str(line)).quantize(Decimal("0.0001")) if line is not None else mapping.line_value or market_line,
        inning_scope=str(existing.get("inning_scope") or mapping.inning_scope or market_scope or _scope(family_key)),
        actual_contract_display=existing.get("actual_contract_display") if isinstance(existing.get("actual_contract_display"), str) else None,
        no_contract_display=existing.get("no_contract_display") if isinstance(existing.get("no_contract_display"), str) else None,
        normalized_no_equivalent_display=(
            existing.get("normalized_no_equivalent_display")
            if isinstance(existing.get("normalized_no_equivalent_display"), str)
            else None
        ),
        parse_source=str(existing.get("parse_source") or "metadata"),
        raw_contract_text=raw_contract_text,
        warnings=list(existing.get("warnings") or []) if isinstance(existing.get("warnings"), list) else [],
        audit_status=audit_status,
        reason_codes=reason_codes,
        yes_interpretation=existing.get("yes_interpretation") if isinstance(existing.get("yes_interpretation"), str) else None,
        no_interpretation=existing.get("no_interpretation") if isinstance(existing.get("no_interpretation"), str) else None,
        no_is_true_complement=bool(existing.get("no_is_true_complement", True)),
        complement_safe_for_paper_settlement=bool(existing.get("complement_safe_for_paper_settlement", True)),
        line_sign=existing.get("line_sign") if isinstance(existing.get("line_sign"), str) else None,
        line_direction=existing.get("line_direction") if isinstance(existing.get("line_direction"), str) else None,
        push_possible=bool(existing.get("push_possible", False)),
        push_condition=existing.get("push_condition") if isinstance(existing.get("push_condition"), str) else None,
        push_rule_verified=bool(existing.get("push_rule_verified", False)),
        condition_type=existing.get("condition_type") if isinstance(existing.get("condition_type"), str) else None,
        threshold_runs=(
            Decimal(str(existing.get("threshold_runs"))).quantize(Decimal("0.0001"))
            if existing.get("threshold_runs") is not None
            else None
        ),
        raw_threshold_runs=(
            Decimal(str(existing.get("raw_threshold_runs"))).quantize(Decimal("0.0001"))
            if existing.get("raw_threshold_runs") is not None
            else None
        ),
        selected_team_margin_required_gt=(
            Decimal(str(existing.get("selected_team_margin_required_gt"))).quantize(Decimal("0.0001"))
            if existing.get("selected_team_margin_required_gt") is not None
            else None
        ),
        display_spread_line=(
            Decimal(str(existing.get("display_spread_line"))).quantize(Decimal("0.0001"))
            if existing.get("display_spread_line") is not None
            else None
        ),
        settlement_formula=existing.get("settlement_formula") if isinstance(existing.get("settlement_formula"), str) else None,
        no_text_source=existing.get("no_text_source") if isinstance(existing.get("no_text_source"), str) else None,
        no_complement_source=(
            existing.get("no_complement_source") if isinstance(existing.get("no_complement_source"), str) else None
        ),
        no_complement_confidence=(
            existing.get("no_complement_confidence")
            if isinstance(existing.get("no_complement_confidence"), str)
            else None
        ),
        ticker_suffix_line_raw=(
            existing.get("ticker_suffix_line_raw")
            if isinstance(existing.get("ticker_suffix_line_raw"), str)
            else None
        ),
    )
    return enforce_spread_family_consistency(
        verification,
        expected_family=family_key,
        ticker=market.ticker if market is not None else None,
    )
