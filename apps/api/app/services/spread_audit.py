from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import KalshiMarket, MarketMapping, MlbGame, ModelCandidate
from app.services.contracts import FIRST_FIVE_SPREAD, FULL_GAME_SPREAD
from app.services.modeling import FAMILY_SCOPE_GOVERNANCE_POLICY
from app.services.probability_adapters import (
    ADAPTER_VERSION_BY_FAMILY,
    CALIBRATION_HOOK_BY_FAMILY,
    PROBABILITY_ADAPTER_FEATURE_POLICY_VERSION,
    PROBABILITY_ADAPTER_POLICY_VERSION,
)
from app.services.spread_verification import SPREAD_AUDIT_STATUSES, SpreadVerification, spread_verification_from_mapping
from app.time_utils import ensure_aware_utc, get_dashboard_zone, today_eastern, utc_now


def _day_bounds(target_date: date) -> tuple[datetime, datetime]:
    local_start = datetime.combine(target_date, time.min, tzinfo=get_dashboard_zone())
    start = ensure_aware_utc(local_start)
    return start, start + timedelta(days=1)


def _status_kind(status: str) -> str:
    lowered = status.strip().lower()
    if any(token in lowered for token in ("cancel", "void")):
        return "void"
    if any(token in lowered for token in ("final", "game over", "completed")):
        return "final"
    return "open"


def _selected_score_pair(game: MlbGame, selection_code: str | None) -> tuple[int, int] | None:
    if game.home_score is None or game.away_score is None:
        return None
    selected = (selection_code or "").upper()
    if selected == (game.home_abbreviation or "").upper():
        return int(game.home_score), int(game.away_score)
    if selected == (game.away_abbreviation or "").upper():
        return int(game.away_score), int(game.home_score)
    return None


def _spread_result(value: Decimal) -> str:
    if value > 0:
        return "win"
    if value < 0:
        return "loss"
    return "push"


def _format_line(value: Decimal | None) -> str | None:
    if value is None:
        return None
    numeric = float(value)
    return f"+{numeric:g}" if numeric > 0 else f"{numeric:g}"


def _preview_formula_fields(game: MlbGame, verification: SpreadVerification) -> dict[str, object]:
    selected_code = verification.selection_code
    opponent_code = None
    if selected_code == (game.home_abbreviation or "").upper():
        opponent_code = (game.away_abbreviation or "").upper() or None
    elif selected_code == (game.away_abbreviation or "").upper():
        opponent_code = (game.home_abbreviation or "").upper() or None
    return {
        "selected_team": selected_code,
        "opponent_team": opponent_code,
        "threshold_runs": str(verification.threshold_runs) if verification.threshold_runs is not None else None,
        "selected_margin_required_gt": (
            str(verification.selected_team_margin_required_gt)
            if verification.selected_team_margin_required_gt is not None
            else None
        ),
        "normalized_spread_line_display": _format_line(verification.display_spread_line or verification.line_value),
        "yes_condition": verification.settlement_formula,
        "condition_type": verification.condition_type,
    }


def _settlement_preview(game: MlbGame, verification: SpreadVerification) -> dict[str, object]:
    selection_code = verification.selection_code
    line_value = verification.line_value
    formula_fields = _preview_formula_fields(game, verification)
    if verification.inning_scope == "first_five":
        return {"preview_status": "first_five_requires_official_linescore", **formula_fields}
    if _status_kind(game.status) == "void":
        return {"preview_status": "void_game", **formula_fields}
    if _status_kind(game.status) != "final":
        return {"preview_status": "pending_final", **formula_fields}
    if line_value is None:
        return {"preview_status": "missing_line", **formula_fields}
    scores = _selected_score_pair(game, selection_code)
    if scores is None:
        return {"preview_status": "missing_score_or_selection", **formula_fields}
    selected_runs, opponent_runs = scores
    margin = selected_runs - opponent_runs
    if (
        verification.condition_type == "team_wins_by_more_than"
        and verification.selected_team_margin_required_gt is not None
    ):
        threshold = verification.selected_team_margin_required_gt
        adjusted_margin = Decimal(margin) - threshold
        if Decimal(margin) > threshold:
            yes_outcome = "win"
            no_outcome = "loss"
            push = False
        elif Decimal(margin) == threshold and verification.push_possible and verification.push_rule_verified:
            yes_outcome = "push"
            no_outcome = "push"
            push = True
        else:
            yes_outcome = "loss"
            no_outcome = "win"
            push = False
    else:
        adjusted_margin = Decimal(margin) + line_value
        yes_outcome = _spread_result(adjusted_margin)
        no_outcome = yes_outcome
        if yes_outcome == "win":
            no_outcome = "loss"
        elif yes_outcome == "loss":
            no_outcome = "win"
        push = yes_outcome == "push"
    return {
        "preview_status": "computed",
        "selected_team_score": selected_runs,
        "opponent_score": opponent_runs,
        "line_value": str(line_value),
        "selected_team_margin": margin,
        "line_adjusted_margin": str(adjusted_margin.quantize(Decimal("0.0001"))),
        "yes_outcome": yes_outcome,
        "no_outcome": no_outcome,
        "push": push,
        **formula_fields,
    }


def _status_counter_template() -> dict[str, int]:
    return {status: 0 for status in sorted(SPREAD_AUDIT_STATUSES)}


def _coverage_status(
    *,
    target_date_mapping_count: int,
    in_window_mapping_count: int,
    checked: int,
) -> str:
    if target_date_mapping_count == 0:
        return "no_target_date_mappings"
    if in_window_mapping_count == 0:
        return "no_mappings_in_window"
    if checked == 0:
        return "zero_checked_with_eligible_mappings"
    if checked < in_window_mapping_count:
        return "partial_coverage"
    if checked == in_window_mapping_count:
        return "covered"
    return "unknown"


def _zero_checked_reason(
    *,
    target_date_mapping_count: int,
    in_window_mapping_count: int,
    checked: int,
) -> str:
    if checked > 0:
        return "not_applicable"
    if target_date_mapping_count == 0:
        return "no_target_date_mappings"
    if in_window_mapping_count == 0:
        return "all_target_date_mappings_outside_window"
    return "eligible_mappings_but_none_checked"


def _append_example(examples: dict[str, list[dict[str, object]]], reason: str, item: dict[str, object]) -> None:
    bucket = examples.setdefault(reason, [])
    if len(bucket) >= 3:
        return
    bucket.append(
        {
            "market_ticker": item.get("market_ticker"),
            "game": item.get("game"),
            "audit_status": item.get("audit_status"),
            "reason_codes": item.get("reason_codes", []),
        }
    )


def run_spread_audit(
    session: Session,
    target_date: date | None = None,
    *,
    min_time_to_start_minutes: int | None = 45,
    max_time_to_start_minutes: int | None = 180,
    family_key: str = FULL_GAME_SPREAD,
) -> dict[str, object]:
    if family_key not in {FULL_GAME_SPREAD, FIRST_FIVE_SPREAD}:
        raise ValueError(f"Unsupported spread audit family: {family_key}")
    day = target_date or today_eastern()
    start, end = _day_bounds(day)
    now = utc_now()
    ticker_prefix = "KXMLBF5SPREAD-%" if family_key == FIRST_FIVE_SPREAD else "KXMLBSPREAD-%"
    rows = list(
        session.execute(
            select(MarketMapping, MlbGame, KalshiMarket)
            .join(MlbGame, MarketMapping.mlb_game_id == MlbGame.id)
            .join(KalshiMarket, MarketMapping.kalshi_market_id == KalshiMarket.id)
            .where(MlbGame.scheduled_start >= start)
            .where(MlbGame.scheduled_start < end)
            .where(
                (MarketMapping.market_family == family_key)
                | (MarketMapping.market_type == family_key)
                | (KalshiMarket.market_family == family_key)
                | (KalshiMarket.market_type == family_key)
                | (KalshiMarket.ticker.ilike(ticker_prefix))
            )
            .order_by(MlbGame.scheduled_start.asc(), KalshiMarket.ticker.asc())
        )
    )

    by_family: dict[str, dict[str, int]] = {}
    items: list[dict[str, object]] = []
    examples_by_reason: dict[str, list[dict[str, object]]] = {}
    status_counts = _status_counter_template()
    checked = verified = unverified = 0
    skipped_before_min_window_count = 0
    skipped_after_max_window_count = 0
    target_date_mapping_count = len(rows)
    target_date_distinct_market_count = len({market.id for _, _, market in rows})
    target_date_distinct_game_count = len({game.id for _, game, _ in rows})
    in_window_mapping_count = 0

    for mapping, game, market in rows:
        minutes_to_start = int((ensure_aware_utc(game.scheduled_start) - now).total_seconds() / 60)
        if min_time_to_start_minutes is not None and minutes_to_start < min_time_to_start_minutes:
            skipped_before_min_window_count += 1
            continue
        if max_time_to_start_minutes is not None and minutes_to_start > max_time_to_start_minutes:
            skipped_after_max_window_count += 1
            continue
        in_window_mapping_count += 1

        family = mapping.market_family or market.market_family or market.market_type or "unknown"
        family_counts = by_family.setdefault(family, {"checked": 0, "verified": 0, "unverified": 0})
        result = spread_verification_from_mapping(game=game, mapping=mapping, market=market)
        metadata = result.as_metadata()
        settlement_preview = _settlement_preview(game, result)

        checked += 1
        family_counts["checked"] += 1
        if result.verified:
            verified += 1
            family_counts["verified"] += 1
        else:
            unverified += 1
            family_counts["unverified"] += 1
        status_counts[result.audit_status] = status_counts.get(result.audit_status, 0) + 1

        item = {
            "mapping_id": mapping.id,
            "market_id": market.id,
            "market_ticker": market.ticker,
            "event_ticker": market.event_ticker,
            "game_id": game.id,
            "game": f"{game.away_abbreviation or game.away_team} @ {game.home_abbreviation or game.home_team}",
            "mapped_mlb_game": {
                "external_game_id": game.external_game_id,
                "away_team": game.away_team,
                "away_abbreviation": game.away_abbreviation,
                "home_team": game.home_team,
                "home_abbreviation": game.home_abbreviation,
                "status": game.status,
                "away_score": game.away_score,
                "home_score": game.home_score,
            },
            "scheduled_start": ensure_aware_utc(game.scheduled_start).isoformat(),
            "minutes_to_start": minutes_to_start,
            "family": family,
            "market_family": family_key,
            "inning_scope": result.inning_scope,
            "audit_status": result.audit_status,
            "reason_codes": list(result.reason_codes or []),
            "verified": result.verified,
            "trusted_audit_only": result.audit_status == "trusted_audit_only",
            "parser_status": result.parser_status,
            "settlement_rule_status": result.settlement_rule_status,
            "selection_code": result.selection_code,
            "selected_team": result.selection_code,
            "line_value": float(result.line_value) if result.line_value is not None else None,
            "line_value_raw": str(result.line_value) if result.line_value is not None else None,
            "line_sign": result.line_sign,
            "line_direction": result.line_direction,
            "condition_type": result.condition_type,
            "rules_threshold_runs": str(result.threshold_runs) if result.threshold_runs is not None else None,
            "raw_threshold_runs": str(result.raw_threshold_runs) if result.raw_threshold_runs is not None else None,
            "selected_team_margin_required_gt": (
                str(result.selected_team_margin_required_gt)
                if result.selected_team_margin_required_gt is not None
                else None
            ),
            "display_spread_line": str(result.display_spread_line) if result.display_spread_line is not None else None,
            "settlement_formula": result.settlement_formula,
            "ticker_suffix_line_raw": result.ticker_suffix_line_raw,
            "over_under_side": None,
            "title": market.title,
            "subtitle": market.subtitle,
            "rules": market.rules,
            "yes_subtitle": market.yes_subtitle,
            "no_subtitle": market.no_subtitle,
            "raw_contract_text": metadata["raw_contract_text"],
            "actual_contract_display": result.actual_contract_display,
            "no_contract_display": result.no_contract_display,
            "normalized_no_equivalent_display": result.normalized_no_equivalent_display,
            "yes_outcome_interpretation": result.yes_interpretation,
            "no_outcome_interpretation": result.no_interpretation,
            "no_is_true_complement": result.no_is_true_complement,
            "no_text_source": result.no_text_source,
            "no_complement_source": result.no_complement_source,
            "no_complement_confidence": result.no_complement_confidence,
            "complement_safe_for_paper_settlement": result.complement_safe_for_paper_settlement,
            "push_possible": result.push_possible,
            "push_condition": result.push_condition,
            "push_rule_verified": result.push_rule_verified,
            "settlement_preview": settlement_preview,
            "parse_source": result.parse_source,
            "warnings": result.warnings,
        }
        for reason in result.reason_codes or []:
            _append_example(examples_by_reason, reason, item)
        items.append(item)

    skipped_by_window = skipped_before_min_window_count + skipped_after_max_window_count
    coverage_ratio = round(checked / in_window_mapping_count, 4) if in_window_mapping_count else None
    coverage_status = _coverage_status(
        target_date_mapping_count=target_date_mapping_count,
        in_window_mapping_count=in_window_mapping_count,
        checked=checked,
    )
    zero_checked_reason = _zero_checked_reason(
        target_date_mapping_count=target_date_mapping_count,
        in_window_mapping_count=in_window_mapping_count,
        checked=checked,
    )
    return {
        "status": "completed",
        "target_date": day.isoformat(),
        "min_time_to_start_minutes": min_time_to_start_minutes,
        "max_time_to_start_minutes": max_time_to_start_minutes,
        "target_date_mapping_count": target_date_mapping_count,
        "target_date_distinct_market_count": target_date_distinct_market_count,
        "target_date_distinct_game_count": target_date_distinct_game_count,
        "in_window_mapping_count": in_window_mapping_count,
        "checked": checked,
        "verified": verified,
        "unverified": unverified,
        "skipped_by_window": skipped_by_window,
        "skipped_before_min_window_count": skipped_before_min_window_count,
        "skipped_after_max_window_count": skipped_after_max_window_count,
        "coverage_ratio": coverage_ratio,
        "coverage_status": coverage_status,
        "zero_checked_reason": zero_checked_reason,
        "by_family": by_family,
        "audit_scope": family_key,
        "audit_only": True,
        "read_only": True,
        "mapping_mutations": 0,
        "candidate_mutations": 0,
        "settlement_rows_created": 0,
        f"total_{family_key}_markets_seen": checked,
        "mapped_to_games": checked,
        "status_counts": status_counts,
        "trusted_audit_only_count": status_counts.get("trusted_audit_only", 0),
        "needs_review_count": status_counts.get("needs_review", 0),
        "unsafe_count": status_counts.get("unsafe", 0),
        "parse_error_count": status_counts.get("parse_error", 0),
        "missing_line_count": status_counts.get("missing_line", 0),
        "ambiguous_team_selection_count": status_counts.get("ambiguous_team_selection", 0),
        "ambiguous_yes_no_semantics_count": status_counts.get("ambiguous_yes_no_semantics", 0),
        "ambiguous_line_direction_count": status_counts.get("ambiguous_line_direction", 0),
        "push_behavior_uncertain_count": status_counts.get("push_behavior_uncertain", 0),
        "settlement_text_unverified_count": status_counts.get("settlement_text_unverified", 0),
        "examples_by_reason": examples_by_reason,
        "items": items[:100],
        "paper_trades_created": 0,
    }


def run_first_five_spread_audit(
    session: Session,
    target_date: date | None = None,
    *,
    min_time_to_start_minutes: int | None = 45,
    max_time_to_start_minutes: int | None = 360,
) -> dict[str, object]:
    return run_spread_audit(
        session,
        target_date,
        min_time_to_start_minutes=min_time_to_start_minutes,
        max_time_to_start_minutes=max_time_to_start_minutes,
        family_key=FIRST_FIVE_SPREAD,
    )


def first_five_spread_adapter_repair_preview(
    session: Session,
    *,
    target_date: date | None = None,
    limit: int = 500,
) -> dict[str, object]:
    bounded_limit = max(1, min(int(limit), 1000))
    family_filter = or_(
        ModelCandidate.market_family == FIRST_FIVE_SPREAD,
        ModelCandidate.market_type == FIRST_FIVE_SPREAD,
    )
    query = (
        select(
            ModelCandidate.id,
            ModelCandidate.target_date,
            ModelCandidate.market_family,
            ModelCandidate.market_type,
            ModelCandidate.inning_scope,
            ModelCandidate.probability_adapter_key,
            ModelCandidate.probability_adapter_family,
            ModelCandidate.probability_adapter_scope,
            ModelCandidate.probability_adapter_policy_version,
            ModelCandidate.probability_adapter_version,
            ModelCandidate.probability_adapter_calibration_hook,
            ModelCandidate.probability_adapter_calibration_version,
            ModelCandidate.probability_adapter_feature_policy_version,
            ModelCandidate.probability_adapter_metadata,
            ModelCandidate.gate_diagnostics,
        )
        .where(family_filter)
        .order_by(ModelCandidate.id.asc())
        .limit(bounded_limit + 1)
    )
    if target_date is not None:
        query = query.where(ModelCandidate.target_date == target_date)

    aggregate_query = select(
        func.count(ModelCandidate.id).label("matching_rows_count"),
        func.min(ModelCandidate.target_date).label("affected_start"),
        func.max(ModelCandidate.target_date).label("affected_end"),
    ).where(family_filter)
    if target_date is not None:
        aggregate_query = aggregate_query.where(ModelCandidate.target_date == target_date)
    aggregate = session.execute(aggregate_query).mappings().one()
    rows = list(session.execute(query).mappings())
    truncated = len(rows) > bounded_limit
    rows = rows[:bounded_limit]
    counts = {
        "already_valid": 0,
        "deterministically_repairable": 0,
        "ambiguous": 0,
        "unsupported": 0,
        "missing_source_evidence": 0,
    }
    examples: dict[str, list[dict[str, object]]] = {key: [] for key in counts}
    reason_counts: dict[str, int] = {}

    def adapter_error_reasons(*values: object) -> list[str]:
        found: list[str] = []

        def walk(value: object) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in {"adapter_error", "probability_adapter_error"} and child is not None and child != "":
                        if isinstance(child, dict):
                            reason = next(
                                (
                                    str(child.get(name))
                                    for name in ("reason", "code", "type", "message")
                                    if child.get(name)
                                ),
                                None,
                            ) or "unknown"
                        else:
                            reason = str(child)
                        reason = reason.strip() or "unknown"
                        if reason not in found:
                            found.append(reason)
                    elif key == "adapter_status" and str(child).lower() == "error":
                        if "adapter_status_error" not in found:
                            found.append("adapter_status_error")
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        for value in values:
            walk(value)
        return found

    expected_scalars = {
        "probability_adapter_key": f"{FIRST_FIVE_SPREAD}_probability_adapter",
        "probability_adapter_version": ADAPTER_VERSION_BY_FAMILY[FIRST_FIVE_SPREAD],
        "probability_adapter_policy_version": PROBABILITY_ADAPTER_POLICY_VERSION,
        "probability_adapter_family": FIRST_FIVE_SPREAD,
        "probability_adapter_scope": "first_five",
        "probability_adapter_calibration_hook": CALIBRATION_HOOK_BY_FAMILY[FIRST_FIVE_SPREAD],
        "probability_adapter_feature_policy_version": PROBABILITY_ADAPTER_FEATURE_POLICY_VERSION,
    }
    expected_metadata = {
        "adapter_key": expected_scalars["probability_adapter_key"],
        "adapter_version": expected_scalars["probability_adapter_version"],
        "adapter_policy_version": expected_scalars["probability_adapter_policy_version"],
        "adapter_family": expected_scalars["probability_adapter_family"],
        "adapter_scope": expected_scalars["probability_adapter_scope"],
        "calibration_hook": expected_scalars["probability_adapter_calibration_hook"],
        "feature_policy_version": expected_scalars["probability_adapter_feature_policy_version"],
    }
    supported_hook_statuses = {"family_scope_active", "metadata_only_pending_pr3v"}
    required_trust_reasons = {
        "selected_team_verified",
        "binary_yes_no_complement_verified",
        "settlement_formula_verified",
        "first_five_scope_verified",
        "first_five_official_result_source_verified",
    }
    blocked_reason_tokens = (
        "ambiguous",
        "unsafe",
        "parse",
        "missing",
        "unverified",
        "conflict",
        "uncertain",
        "unsupported",
        "error",
    )

    def family_reason(row: dict[str, object]) -> str | None:
        values = {
            str(value)
            for value in (row.get("market_family"), row.get("market_type"))
            if value not in {None, ""}
        }
        if not values:
            return "missing_market_family"
        if any(value != FIRST_FIVE_SPREAD for value in values):
            return "conflicting_market_family"
        if row.get("inning_scope") not in {None, "first_five"}:
            return "conflicting_inning_scope"
        return None

    def strict_metadata(row: dict[str, object], metadata: dict[str, object]) -> bool:
        if any(row.get(field) != expected for field, expected in expected_scalars.items()):
            return False
        if row.get("inning_scope") != "first_five":
            return False
        if any(metadata.get(field) != expected for field, expected in expected_metadata.items()):
            return False
        calibration_version = metadata.get("calibration_version")
        if not isinstance(calibration_version, str) or not calibration_version.strip():
            return False
        if row.get("probability_adapter_calibration_version") != calibration_version:
            return False
        calibration_hook_status = metadata.get("calibration_hook_status")
        if calibration_hook_status not in supported_hook_statuses:
            return False
        if calibration_hook_status == "metadata_only_pending_pr3v" and calibration_version != "shared_parameter_offsets_pre_pr3v":
            return False
        if calibration_hook_status == "family_scope_active" and not calibration_version.startswith(
            f"{FAMILY_SCOPE_GOVERNANCE_POLICY}_{FIRST_FIVE_SPREAD}_"
        ):
            return False
        metadata_diagnostics = metadata.get("diagnostics")
        if not isinstance(metadata_diagnostics, dict):
            return False
        if adapter_error_reasons(row.get("gate_diagnostics"), metadata):
            return False
        return True

    def strict_verification(diagnostics: dict[str, object]) -> bool:
        verification = diagnostics.get("spread_verification")
        if not isinstance(verification, dict):
            return False
        if verification.get("verified") is not True or verification.get("audit_status") != "trusted_audit_only":
            return False
        if verification.get("family_key") != FIRST_FIVE_SPREAD or verification.get("inning_scope") != "first_five":
            return False
        if not verification.get("selection_code") or not verification.get("settlement_formula"):
            return False
        try:
            if Decimal(str(verification.get("line_value"))) == Decimal("0"):
                return False
        except (ArithmeticError, TypeError, ValueError):
            return False
        if verification.get("no_is_true_complement") is not True:
            return False
        if verification.get("complement_safe_for_paper_settlement") is not True:
            return False
        push_possible = verification.get("push_possible")
        if push_possible is not False and verification.get("push_rule_verified") is not True:
            return False
        reason_codes = verification.get("reason_codes")
        if not isinstance(reason_codes, list) or not required_trust_reasons.issubset(set(reason_codes)):
            return False
        if any(
            any(token in str(reason).lower() for token in blocked_reason_tokens)
            for reason in reason_codes
        ):
            return False
        return True

    for row in rows:
        diagnostics = row.get("gate_diagnostics")
        diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
        adapter_metadata = row.get("probability_adapter_metadata")
        adapter_metadata = adapter_metadata if isinstance(adapter_metadata, dict) else {}
        error_reasons = adapter_error_reasons(diagnostics, adapter_metadata)
        if error_reasons:
            for error_reason in error_reasons:
                reason_counts[f"adapter_error:{error_reason}"] = reason_counts.get(
                    f"adapter_error:{error_reason}", 0
                ) + 1
            reason = f"adapter_error:{error_reasons[0]}"
            classification = "ambiguous"
        elif family_reason(row) is not None:
            reason = family_reason(row) or "unsupported_adapter_family"
            classification = "unsupported"
        elif strict_metadata(row, adapter_metadata):
            reason = "complete_adapter_metadata"
            classification = "already_valid"
        elif strict_verification(diagnostics):
            reason = "deterministic_spread_verification_evidence"
            classification = "deterministically_repairable"
        elif isinstance(diagnostics.get("spread_verification"), dict):
            reason = "untrusted_spread_verification"
            classification = "ambiguous"
        else:
            reason = "missing_source_evidence"
            classification = "missing_source_evidence"
        counts[classification] += 1
        if not error_reasons:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        if len(examples[classification]) < 3:
            examples[classification].append(
                {
                    "candidate_id": row.get("id"),
                    "target_date": row.get("target_date").isoformat() if row.get("target_date") else None,
                    "adapter_family": row.get("probability_adapter_family"),
                    "adapter_scope": row.get("probability_adapter_scope"),
                }
            )
    return {
        "status": "completed",
        "target_date": target_date.isoformat() if target_date else None,
        "preview_only": True,
        "read_only": True,
        "mutations_applied": 0,
        "candidate_mutations": 0,
        "bounded_limit": bounded_limit,
        "rows_seen": len(rows),
        "matching_rows_count": int(aggregate["matching_rows_count"] or 0),
        "truncated": truncated,
        "classification_counts": counts,
        "reason_counts": reason_counts,
        "affected_target_date_range": {
            "start": aggregate["affected_start"].isoformat() if aggregate["affected_start"] else None,
            "end": aggregate["affected_end"].isoformat() if aggregate["affected_end"] else None,
        },
        "affected_target_date_range_complete": True,
        "examples": examples,
    }
