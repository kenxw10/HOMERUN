from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from app.models import ModelCandidate
from app.services.contracts import FIRST_FIVE_SPREAD, FIRST_FIVE_TOTAL, FULL_GAME_SPREAD, FULL_GAME_TOTAL

LEGACY_PR3W_PROBABILITY_HARDENING_POLICY_VERSION = "pr3w_tail_alternate_probability_hardening_v1"
LEGACY_PR3W_LINE_CLASS_POLICY = "pr3w_line_class_edge_dampening_v1"
PROBABILITY_HARDENING_POLICY_VERSION = "pr4c_one_way_conservative_total_tail_hardening_v1"
PROBABILITY_HARDENING_LINE_CLASS_POLICY = "pr4c_one_way_conservative_total_tail_hardening_v1"

LINE_MARKET_FAMILIES = {FULL_GAME_SPREAD, FULL_GAME_TOTAL, FIRST_FIVE_SPREAD, FIRST_FIVE_TOTAL}
TOTAL_FAMILIES = {FULL_GAME_TOTAL, FIRST_FIVE_TOTAL}
SPREAD_FAMILIES = {FULL_GAME_SPREAD, FIRST_FIVE_SPREAD}

DAMPENING_BY_LINE_CLASS: dict[str, Decimal] = {
    "not_applicable": Decimal("1.0000"),
    "central": Decimal("1.0000"),
    "near_alternate": Decimal("0.9000"),
    "deep_alternate": Decimal("0.7500"),
    "tail": Decimal("0.5000"),
    "unclassified": Decimal("0.5000"),
}
MONOTONICITY_TOLERANCE = Decimal("0.020000")
COMPLEMENT_TOLERANCE = Decimal("0.080000")
DEFAULT_ANCHOR_PROBABILITY = Decimal("0.500000")
MIN_PROBABILITY = Decimal("0.010000")
MAX_PROBABILITY = Decimal("0.990000")
DEFAULT_TOTAL_TAIL_MAX_RAW_ADAPTER_LIFT_ABS = Decimal("0.05")
DEFAULT_TOTAL_DEEP_ALT_MAX_RAW_ADAPTER_LIFT_ABS = Decimal("0.05")
DEFAULT_TOTAL_TAIL_MAX_RAW_ADAPTER_MULTIPLIER = Decimal("1.50")
DEFAULT_TOTAL_DEEP_ALT_MAX_RAW_ADAPTER_MULTIPLIER = Decimal("1.75")

PROBABILITY_HARDENING_CANDIDATE_FIELDS = (
    "probability_hardening_policy_version",
    "probability_hardening_enabled",
    "probability_raw_adapter",
    "probability_before_hardening",
    "probability_after_hardening",
    "probability_hardening_delta",
    "probability_hardening_applied",
    "probability_hardening_reason",
    "probability_hardening_status",
    "probability_hardening_line_class",
    "probability_hardening_line_class_policy",
    "probability_hardening_consistency_status",
    "probability_hardening_monotonicity_status",
    "probability_hardening_ladder_role",
    "probability_hardening_ladder_size",
    "probability_hardening_ladder_rank",
    "probability_hardening_distance_from_central",
    "probability_hardening_central_reference_line",
    "probability_hardening_central_reference_probability",
    "probability_hardening_dampening_factor",
    "probability_hardening_shadow_only",
    "probability_hardening_block_recommendation",
    "probability_hardening_error_reason",
)


def apply_probability_hardening(
    candidates: Iterable[ModelCandidate],
    *,
    enabled: bool,
    policy_version: str = PROBABILITY_HARDENING_POLICY_VERSION,
    total_tail_max_raw_adapter_lift_abs: Decimal = DEFAULT_TOTAL_TAIL_MAX_RAW_ADAPTER_LIFT_ABS,
    total_deep_alt_max_raw_adapter_lift_abs: Decimal = DEFAULT_TOTAL_DEEP_ALT_MAX_RAW_ADAPTER_LIFT_ABS,
    total_tail_max_raw_adapter_multiplier: Decimal = DEFAULT_TOTAL_TAIL_MAX_RAW_ADAPTER_MULTIPLIER,
    total_deep_alt_max_raw_adapter_multiplier: Decimal = DEFAULT_TOTAL_DEEP_ALT_MAX_RAW_ADAPTER_MULTIPLIER,
) -> None:
    rows = list(candidates)
    monotonicity = _monotonicity_status_by_candidate(rows)
    consistency = _consistency_status_by_candidate(rows)
    anchors = _central_anchor_by_group(rows)
    line_class_policy = _line_class_policy_for_policy(policy_version)
    pr4c_enabled = _is_pr4c_policy(policy_version)

    for candidate in rows:
        before = _probability(candidate.probability_calibrated) or _probability(candidate.probability)
        raw_adapter = _probability(candidate.probability_raw)
        line_class = _line_class(candidate)
        family = _family(candidate)
        line_group = _line_group_key(candidate)
        anchor_line, anchor_probability, anchor_role = anchors.get(
            line_group, (None, DEFAULT_ANCHOR_PROBABILITY, "default_midpoint_anchor")
        )
        consistency_status = consistency.get(id(candidate), "not_applicable")
        monotonicity_status = monotonicity.get(id(candidate), "not_applicable")
        factor = DAMPENING_BY_LINE_CLASS.get(line_class, DAMPENING_BY_LINE_CLASS["unclassified"])
        status = "not_applicable" if family not in LINE_MARKET_FAMILIES else "available"
        reason = _reason_for_line_class(line_class)
        error_reason: str | None = None
        shadow_only = False
        block_recommendation = False

        if not enabled:
            status = "disabled"
            reason = "probability_hardening_disabled"
            factor = Decimal("1.0000")
        elif before is None:
            status = "error"
            reason = "missing_probability"
            error_reason = "missing_probability"
            factor = Decimal("1.0000")
        elif family not in LINE_MARKET_FAMILIES:
            factor = Decimal("1.0000")
        else:
            if line_class == "unclassified" and candidate.line_class_reason == "insufficient_kalshi_ladder_depth":
                factor = Decimal("1.0000")
                status = "insufficient_ladder"
                reason = "insufficient_ladder_depth_no_probability_hardening"
            elif line_class == "unclassified":
                shadow_only = True
                block_recommendation = True
                reason = "unclassified_line_shadow_only"
            elif line_class == "tail":
                shadow_only = True
                block_recommendation = True
                reason = "tail_line_requires_exceptional_edge_after_hardening"

            if consistency_status == "failed":
                factor = min(factor, Decimal("0.5000"))
                status = "failed"
                reason = "line_probability_consistency_failed"
                shadow_only = True
                block_recommendation = True
                error_reason = "complement_probability_inconsistent"
            if monotonicity_status == "failed":
                factor = min(factor, Decimal("0.5000"))
                status = "failed"
                reason = "line_probability_monotonicity_failed"
                shadow_only = True
                block_recommendation = True
                error_reason = "ladder_probability_non_monotonic"

        after = before
        if before is not None and enabled and family in LINE_MARKET_FAMILIES:
            anchor_hardened = _harden_probability(before, anchor_probability, factor)
            after = anchor_hardened
            if pr4c_enabled and _is_total_extreme_line(family, line_class):
                after, guardrail_reason, guardrail_status, guardrail_error, guardrail_shadow = (
                    _apply_pr4c_total_extreme_guardrails(
                        candidate=candidate,
                        before=before,
                        anchor_hardened=anchor_hardened,
                        raw_adapter=raw_adapter,
                        line_class=line_class,
                        tail_abs_lift=total_tail_max_raw_adapter_lift_abs,
                        deep_alt_abs_lift=total_deep_alt_max_raw_adapter_lift_abs,
                        tail_multiplier=total_tail_max_raw_adapter_multiplier,
                        deep_alt_multiplier=total_deep_alt_max_raw_adapter_multiplier,
                    )
                )
                if guardrail_reason:
                    reason = guardrail_reason if status != "failed" else reason
                if guardrail_status and status != "failed":
                    status = guardrail_status
                if guardrail_error and status != "failed":
                    error_reason = guardrail_error
                if guardrail_shadow:
                    shadow_only = True
                    block_recommendation = True
        applied = before is not None and after is not None and after != before

        candidate.probability_hardening_policy_version = policy_version
        candidate.probability_hardening_enabled = enabled
        candidate.probability_raw_adapter = raw_adapter
        candidate.probability_before_hardening = before
        candidate.probability_after_hardening = after
        candidate.probability_hardening_delta = (
            (after - before).quantize(Decimal("0.000001")) if before is not None and after is not None else None
        )
        candidate.probability_hardening_applied = applied
        candidate.probability_hardening_reason = reason
        candidate.probability_hardening_status = status
        candidate.probability_hardening_line_class = line_class
        candidate.probability_hardening_line_class_policy = line_class_policy
        candidate.probability_hardening_consistency_status = consistency_status
        candidate.probability_hardening_monotonicity_status = monotonicity_status
        candidate.probability_hardening_ladder_role = anchor_role
        candidate.probability_hardening_ladder_size = candidate.line_ladder_size
        candidate.probability_hardening_ladder_rank = candidate.line_ladder_rank
        candidate.probability_hardening_distance_from_central = candidate.line_ladder_distance_from_central
        candidate.probability_hardening_central_reference_line = anchor_line
        candidate.probability_hardening_central_reference_probability = anchor_probability
        candidate.probability_hardening_dampening_factor = factor
        candidate.probability_hardening_shadow_only = shadow_only
        candidate.probability_hardening_block_recommendation = block_recommendation
        candidate.probability_hardening_error_reason = error_reason


def finalize_probability_hardening_recommendation(
    candidate: ModelCandidate,
    *,
    exceptional_min_net_ev: Decimal,
    exceptional_min_prob_edge: Decimal,
    pr4c_total_tail_min_net_ev: Decimal | None = None,
    pr4c_total_tail_min_prob_edge: Decimal | None = None,
) -> None:
    if not candidate.probability_hardening_enabled:
        return
    line_class = candidate.probability_hardening_line_class
    status = candidate.probability_hardening_status
    pr4c_total_tail = (
        _is_pr4c_policy(str(candidate.probability_hardening_policy_version or ""))
        and _family(candidate) in TOTAL_FAMILIES
        and line_class == "tail"
    )
    required_net_ev = exceptional_min_net_ev
    required_prob_edge = exceptional_min_prob_edge
    if pr4c_total_tail:
        required_net_ev = max(required_net_ev, pr4c_total_tail_min_net_ev or required_net_ev)
        required_prob_edge = max(required_prob_edge, pr4c_total_tail_min_prob_edge or required_prob_edge)
    exceptional = (
        candidate.net_expected_value is not None
        and candidate.probability_edge is not None
        and candidate.net_expected_value >= required_net_ev
        and candidate.probability_edge >= required_prob_edge
    )
    if line_class == "tail" and status in {"failed", "missing_raw_adapter", "error"}:
        candidate.probability_hardening_shadow_only = True
        candidate.probability_hardening_block_recommendation = True
    elif line_class == "tail" and exceptional:
        candidate.probability_hardening_shadow_only = False
        candidate.probability_hardening_block_recommendation = False
        candidate.probability_hardening_reason = (
            "total_tail_exceptional_threshold_met" if pr4c_total_tail else "tail_line_exceptional_edge_after_hardening"
        )
    elif line_class == "tail":
        candidate.probability_hardening_shadow_only = True
        candidate.probability_hardening_block_recommendation = True
        if pr4c_total_tail and candidate.probability_hardening_reason not in {
            "total_tail_raw_adapter_guardrail_applied",
            "total_tail_one_way_cap_applied",
            "total_tail_missing_raw_adapter",
        }:
            candidate.probability_hardening_reason = "total_tail_exceptional_threshold_not_met"
    elif line_class == "unclassified" and candidate.probability_hardening_status != "insufficient_ladder":
        candidate.probability_hardening_shadow_only = True
        candidate.probability_hardening_block_recommendation = True


def probability_hardening_candidate_payload(candidate: ModelCandidate) -> dict[str, object]:
    return {
        "probability_hardening_policy_version": candidate.probability_hardening_policy_version,
        "probability_hardening_enabled": candidate.probability_hardening_enabled,
        "probability_raw_adapter": _decimal_float(candidate.probability_raw_adapter),
        "probability_before_hardening": _decimal_float(candidate.probability_before_hardening),
        "probability_after_hardening": _decimal_float(candidate.probability_after_hardening),
        "probability_hardening_delta": _decimal_float(candidate.probability_hardening_delta),
        "probability_hardening_applied": candidate.probability_hardening_applied,
        "probability_hardening_reason": candidate.probability_hardening_reason,
        "probability_hardening_status": candidate.probability_hardening_status,
        "probability_hardening_line_class": candidate.probability_hardening_line_class,
        "probability_hardening_line_class_policy": candidate.probability_hardening_line_class_policy,
        "probability_hardening_consistency_status": candidate.probability_hardening_consistency_status,
        "probability_hardening_monotonicity_status": candidate.probability_hardening_monotonicity_status,
        "probability_hardening_ladder_role": candidate.probability_hardening_ladder_role,
        "probability_hardening_ladder_size": candidate.probability_hardening_ladder_size,
        "probability_hardening_ladder_rank": candidate.probability_hardening_ladder_rank,
        "probability_hardening_distance_from_central": candidate.probability_hardening_distance_from_central,
        "probability_hardening_central_reference_line": _decimal_float(
            candidate.probability_hardening_central_reference_line
        ),
        "probability_hardening_central_reference_probability": _decimal_float(
            candidate.probability_hardening_central_reference_probability
        ),
        "probability_hardening_dampening_factor": _decimal_float(candidate.probability_hardening_dampening_factor),
        "probability_hardening_shadow_only": candidate.probability_hardening_shadow_only,
        "probability_hardening_block_recommendation": candidate.probability_hardening_block_recommendation,
        "probability_hardening_error_reason": candidate.probability_hardening_error_reason,
    }


def probability_hardening_field_counts(candidates: Iterable[ModelCandidate]) -> dict[str, int]:
    counts = {field: 0 for field in PROBABILITY_HARDENING_CANDIDATE_FIELDS}
    for candidate in candidates:
        payload = probability_hardening_candidate_payload(candidate)
        for field, value in payload.items():
            if value is not None:
                counts[field] += 1
    return counts


def probability_hardening_summary(
    candidates: Iterable[ModelCandidate],
    *,
    enabled: bool,
    policy_version: str = PROBABILITY_HARDENING_POLICY_VERSION,
) -> dict[str, object]:
    by_line_class: dict[str, dict[str, int]] = {}
    by_family_scope: dict[str, dict[str, int]] = {}
    consistency_counts: dict[str, int] = {}
    monotonicity_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    applied_count = 0
    shadow_count = 0
    block_count = 0
    error_count = 0
    missing_count = 0
    for candidate in candidates:
        status = candidate.probability_hardening_status or "missing"
        line_class = candidate.probability_hardening_line_class or "missing"
        family_scope = f"{_family(candidate) or 'unknown'}:{candidate.inning_scope or _scope_for_family(_family(candidate))}"
        status_counts[status] = status_counts.get(status, 0) + 1
        if candidate.probability_hardening_reason:
            reason = candidate.probability_hardening_reason
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        if candidate.probability_hardening_applied:
            applied_count += 1
        if candidate.probability_hardening_shadow_only:
            shadow_count += 1
        if candidate.probability_hardening_block_recommendation:
            block_count += 1
        if candidate.probability_hardening_error_reason:
            error_count += 1
        if not candidate.probability_hardening_policy_version:
            missing_count += 1
        _increment_nested(by_line_class, line_class, status)
        _increment_nested(by_family_scope, family_scope, status)
        consistency_status = candidate.probability_hardening_consistency_status or "missing"
        monotonicity_status = candidate.probability_hardening_monotonicity_status or "missing"
        consistency_counts[consistency_status] = consistency_counts.get(consistency_status, 0) + 1
        monotonicity_counts[monotonicity_status] = monotonicity_counts.get(monotonicity_status, 0) + 1
    return {
        "probability_hardening_policy_version": policy_version,
        "probability_hardening_enabled": enabled,
        "probability_hardening_line_class_policy": _line_class_policy_for_policy(policy_version),
        "probability_hardening_applied_count": applied_count,
        "probability_hardening_shadow_only_count": shadow_count,
        "probability_hardening_block_recommendation_count": block_count,
        "probability_hardening_error_count": error_count,
        "probability_hardening_missing_count": missing_count,
        "probability_hardening_status_counts": status_counts,
        "probability_hardening_reason_counts": reason_counts,
        "probability_hardening_by_line_class": by_line_class,
        "probability_hardening_by_family_scope": by_family_scope,
        "probability_hardening_consistency_status_counts": consistency_counts,
        "probability_hardening_monotonicity_status_counts": monotonicity_counts,
    }


def _monotonicity_status_by_candidate(candidates: list[ModelCandidate]) -> dict[int, str]:
    status = {
        id(candidate): "not_applicable" if _family(candidate) not in LINE_MARKET_FAMILIES else "passed"
        for candidate in candidates
    }
    groups: dict[tuple[object, ...], list[ModelCandidate]] = {}
    for candidate in candidates:
        key = _monotonicity_key(candidate)
        if key is not None:
            groups.setdefault(key, []).append(candidate)

    for group in groups.values():
        ordered = sorted(
            (candidate for candidate in group if _line(candidate) is not None and _probability(candidate.probability_calibrated) is not None),
            key=lambda candidate: _line(candidate) or Decimal("0"),
        )
        if len(ordered) < 2:
            for candidate in group:
                status[id(candidate)] = "insufficient_ladder"
            continue
        direction = str(ordered[0].economic_exposure_direction or "").lower()
        ascending = _expected_probability_ascending(_family(ordered[0]), direction)
        failed = False
        previous_probability: Decimal | None = None
        for candidate in ordered:
            probability = _probability(candidate.probability_calibrated)
            if probability is None:
                continue
            if previous_probability is not None:
                if ascending and probability + MONOTONICITY_TOLERANCE < previous_probability:
                    failed = True
                if not ascending and probability - MONOTONICITY_TOLERANCE > previous_probability:
                    failed = True
            previous_probability = probability
        for candidate in group:
            status[id(candidate)] = "failed" if failed else "passed"
    return status


def _consistency_status_by_candidate(candidates: list[ModelCandidate]) -> dict[int, str]:
    status = {
        id(candidate): "not_applicable" if _family(candidate) not in LINE_MARKET_FAMILIES else "passed"
        for candidate in candidates
    }
    groups: dict[tuple[object, ...], dict[str, ModelCandidate]] = {}
    for candidate in candidates:
        key = _complement_key(candidate)
        direction = str(candidate.economic_exposure_direction or "").lower()
        if key is None or direction not in {"over", "under", "cover", "not_cover"}:
            continue
        groups.setdefault(key, {})[direction] = candidate

    for by_direction in groups.values():
        pairs = (("over", "under"), ("cover", "not_cover"))
        for left, right in pairs:
            if left not in by_direction or right not in by_direction:
                continue
            left_candidate = by_direction[left]
            right_candidate = by_direction[right]
            left_probability = _probability(left_candidate.probability_calibrated)
            right_probability = _probability(right_candidate.probability_calibrated)
            if left_probability is None or right_probability is None:
                continue
            pair_status = "passed"
            if abs((left_probability + right_probability) - Decimal("1.000000")) > COMPLEMENT_TOLERANCE:
                pair_status = "failed"
            status[id(left_candidate)] = pair_status
            status[id(right_candidate)] = pair_status
    return status


def _central_anchor_by_group(
    candidates: list[ModelCandidate],
) -> dict[tuple[object, ...] | None, tuple[Decimal | None, Decimal, str]]:
    anchors: dict[tuple[object, ...] | None, tuple[Decimal | None, Decimal, str]] = {}
    groups: dict[tuple[object, ...] | None, list[ModelCandidate]] = {}
    for candidate in candidates:
        groups.setdefault(_line_group_key(candidate), []).append(candidate)

    for key, group in groups.items():
        central = [
            candidate
            for candidate in group
            if _line_class(candidate) == "central" and _probability(candidate.probability_calibrated) is not None
        ]
        if central:
            selected = sorted(
                central,
                key=lambda candidate: (
                    abs(_probability(candidate.probability_calibrated) - Decimal("0.500000"))
                    if _probability(candidate.probability_calibrated) is not None
                    else Decimal("1"),
                    candidate.line_ladder_rank or 9999,
                ),
            )[0]
            anchors[key] = (
                _line(selected),
                _probability(selected.probability_calibrated) or DEFAULT_ANCHOR_PROBABILITY,
                "current_ladder_central_reference",
            )
            continue
        available = [
            candidate
            for candidate in group
            if _probability(candidate.probability_calibrated) is not None and _line(candidate) is not None
        ]
        if available:
            selected = sorted(
                available,
                key=lambda candidate: (
                    candidate.line_ladder_distance_from_central
                    if candidate.line_ladder_distance_from_central is not None
                    else 9999,
                    candidate.line_ladder_rank or 9999,
                ),
            )[0]
            anchors[key] = (
                _line(selected),
                _probability(selected.probability_calibrated) or DEFAULT_ANCHOR_PROBABILITY,
                "nearest_available_ladder_reference",
            )
            continue
        anchors[key] = (None, DEFAULT_ANCHOR_PROBABILITY, "default_midpoint_anchor")
    return anchors


def _harden_probability(before: Decimal, anchor: Decimal, factor: Decimal) -> Decimal:
    if factor >= Decimal("1"):
        return before.quantize(Decimal("0.000001"))
    hardened = anchor + ((before - anchor) * factor)
    return min(max(hardened, MIN_PROBABILITY), MAX_PROBABILITY).quantize(Decimal("0.000001"))


def _is_pr4c_policy(policy_version: str | None) -> bool:
    return (policy_version or PROBABILITY_HARDENING_POLICY_VERSION) == PROBABILITY_HARDENING_POLICY_VERSION


def _line_class_policy_for_policy(policy_version: str | None) -> str:
    return LEGACY_PR3W_LINE_CLASS_POLICY if policy_version == LEGACY_PR3W_PROBABILITY_HARDENING_POLICY_VERSION else PROBABILITY_HARDENING_LINE_CLASS_POLICY


def _is_total_extreme_line(family: str | None, line_class: str) -> bool:
    return family in TOTAL_FAMILIES and line_class in {"deep_alternate", "tail"}


def _apply_pr4c_total_extreme_guardrails(
    *,
    candidate: ModelCandidate,
    before: Decimal,
    anchor_hardened: Decimal,
    raw_adapter: Decimal | None,
    line_class: str,
    tail_abs_lift: Decimal,
    deep_alt_abs_lift: Decimal,
    tail_multiplier: Decimal,
    deep_alt_multiplier: Decimal,
) -> tuple[Decimal, str | None, str | None, str | None, bool]:
    prefix = "total_tail" if line_class == "tail" else "total_deep_alternate"
    after = min(anchor_hardened, before).quantize(Decimal("0.000001"))
    reason = f"{prefix}_one_way_cap_applied" if anchor_hardened > before else None
    status = "one_way_capped" if reason else None
    error_reason: str | None = None
    shadow_only = False

    if raw_adapter is None:
        error_reason = "missing_raw_adapter"
        status = "missing_raw_adapter"
        if reason is None:
            reason = f"{prefix}_missing_raw_adapter"
        if line_class == "tail":
            shadow_only = True
        return after, reason, status, error_reason, shadow_only

    cap = _raw_adapter_guardrail_cap(
        raw_adapter,
        abs_lift=tail_abs_lift if line_class == "tail" else deep_alt_abs_lift,
        multiplier=tail_multiplier if line_class == "tail" else deep_alt_multiplier,
    )
    if after > cap:
        after = cap
        reason = f"{prefix}_raw_adapter_guardrail_applied"
        status = (
            f"{prefix}_baseline_calibration_guardrail_applied"
            if _uses_baseline_or_shared_calibration(candidate)
            else "raw_adapter_guardrailed"
        )
    return after, reason, status, error_reason, shadow_only


def _raw_adapter_guardrail_cap(raw_adapter: Decimal, *, abs_lift: Decimal, multiplier: Decimal) -> Decimal:
    cap = min(raw_adapter + _nonnegative_decimal(abs_lift), raw_adapter * max(_nonnegative_decimal(multiplier), Decimal("1")))
    return min(max(cap, MIN_PROBABILITY), MAX_PROBABILITY).quantize(Decimal("0.000001"))


def _uses_baseline_or_shared_calibration(candidate: ModelCandidate) -> bool:
    calibration_status = str(candidate.calibration_status or "").lower()
    calibration_version = str(candidate.probability_adapter_calibration_version or "").lower()
    return (
        "baseline" in calibration_status
        or "pre_pr3v" in calibration_version
        or "shared_parameter_offsets" in calibration_version
    )


def _nonnegative_decimal(value: Decimal) -> Decimal:
    return max(_decimal(value) or Decimal("0"), Decimal("0"))


def _line_group_key(candidate: ModelCandidate) -> tuple[object, ...] | None:
    family = _family(candidate)
    if family not in LINE_MARKET_FAMILIES:
        return None
    return (
        candidate.mlb_game_id,
        family,
        candidate.inning_scope or _scope_for_family(family),
        candidate.economic_exposure_family or family,
        candidate.economic_exposure_direction or "unknown",
        candidate.economic_exposure_team or "market",
    )


def _monotonicity_key(candidate: ModelCandidate) -> tuple[object, ...] | None:
    family = _family(candidate)
    line = _line(candidate)
    direction = str(candidate.economic_exposure_direction or "").lower()
    if family not in LINE_MARKET_FAMILIES or line is None or direction not in {"over", "under", "cover", "not_cover"}:
        return None
    return (
        candidate.mlb_game_id,
        family,
        candidate.inning_scope or _scope_for_family(family),
        candidate.economic_exposure_family or family,
        direction,
        candidate.economic_exposure_team or "market",
    )


def _complement_key(candidate: ModelCandidate) -> tuple[object, ...] | None:
    family = _family(candidate)
    line = _line(candidate)
    if family not in LINE_MARKET_FAMILIES or line is None:
        return None
    return (
        candidate.mlb_game_id,
        family,
        candidate.inning_scope or _scope_for_family(family),
        candidate.economic_exposure_family or family,
        candidate.economic_exposure_team or "market",
        line,
    )


def _expected_probability_ascending(family: str | None, direction: str) -> bool:
    if family in TOTAL_FAMILIES:
        return direction == "under"
    if family in SPREAD_FAMILIES:
        return direction == "cover"
    return True


def _reason_for_line_class(line_class: str) -> str:
    return {
        "not_applicable": "market_family_has_no_line_ladder",
        "central": "central_line_no_hardening",
        "near_alternate": "near_alternate_probability_edge_dampened",
        "deep_alternate": "deep_alternate_probability_edge_dampened",
        "tail": "tail_line_requires_exceptional_edge_after_hardening",
        "unclassified": "unclassified_line_shadow_only",
    }.get(line_class, "unknown_line_class_probability_hardened")


def _line_class(candidate: ModelCandidate) -> str:
    return str(candidate.line_class or "not_applicable").lower()


def _family(candidate: ModelCandidate) -> str | None:
    return candidate.market_family or candidate.market_type


def _line(candidate: ModelCandidate) -> Decimal | None:
    return _decimal(candidate.economic_exposure_line if candidate.economic_exposure_line is not None else candidate.line_value)


def _probability(value: Any) -> Decimal | None:
    probability = _decimal(value)
    if probability is None:
        return None
    return min(max(probability, Decimal("0.000000")), Decimal("1.000000")).quantize(Decimal("0.000001"))


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.000001"))
    except (InvalidOperation, ValueError):
        return None


def _decimal_float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _scope_for_family(family: str | None) -> str:
    return "first_five" if (family or "").startswith("first_five") else "full_game"


def _increment_nested(container: dict[str, dict[str, int]], bucket: str, status: str) -> None:
    counts = container.setdefault(bucket, {})
    counts[status] = counts.get(status, 0) + 1
