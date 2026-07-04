from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from app.config import Settings
from app.models import ModelCandidate
from app.services.contracts import (
    FIRST_FIVE_SPREAD,
    FIRST_FIVE_TOTAL,
    FIRST_FIVE_WINNER,
    FULL_GAME_SPREAD,
    FULL_GAME_TOTAL,
    FULL_GAME_WINNER,
)

SELECTOR_POLICY_VERSION = "pr3t_live_like_selector_v1"
SELECTOR_LINE_CLASS_POLICY_VERSION = "pr3t_line_class_selector_v1"

LINE_MARKET_FAMILIES = {FULL_GAME_SPREAD, FULL_GAME_TOTAL, FIRST_FIVE_SPREAD, FIRST_FIVE_TOTAL}

FAMILY_SCOPE_THRESHOLDS: dict[str, tuple[Decimal, Decimal]] = {
    FULL_GAME_WINNER: (Decimal("0.05"), Decimal("0.03")),
    FIRST_FIVE_WINNER: (Decimal("0.055"), Decimal("0.035")),
    FULL_GAME_TOTAL: (Decimal("0.06"), Decimal("0.04")),
    FIRST_FIVE_TOTAL: (Decimal("0.065"), Decimal("0.045")),
    FULL_GAME_SPREAD: (Decimal("0.07"), Decimal("0.045")),
    FIRST_FIVE_SPREAD: (Decimal("0.075"), Decimal("0.05")),
}

LINE_CLASS_PREFERENCE = {
    "not_applicable": 5,
    "central": 5,
    "near_alternate": 4,
    "deep_alternate": 3,
    "tail": 2,
    "unclassified": 1,
}


class SelectorIntent(Protocol):
    candidate: ModelCandidate
    price: Decimal
    score: Decimal


@dataclass(frozen=True)
class SelectorThreshold:
    profile: str
    min_net_ev: Decimal
    min_prob_edge: Decimal
    min_data_quality: Decimal
    line_class_policy: str
    shadow_only: bool
    line_class: str


def _quantized(value: Decimal, places: str = "0.000001") -> Decimal:
    return value.quantize(Decimal(places))


def _line_class(candidate: ModelCandidate) -> str:
    line_class = (candidate.line_class or "").strip().lower()
    if line_class:
        return line_class
    family = candidate.market_family or candidate.market_type or ""
    return "unclassified" if family in LINE_MARKET_FAMILIES else "not_applicable"


def selector_threshold(candidate: ModelCandidate, price: Decimal | None, settings: Settings) -> SelectorThreshold:
    family = candidate.market_family or candidate.market_type or FULL_GAME_WINNER
    base_ev, base_edge = FAMILY_SCOPE_THRESHOLDS.get(
        family,
        (settings.paper_min_net_ev, settings.paper_min_prob_edge),
    )
    min_ev = max(settings.paper_min_net_ev, base_ev)
    min_edge = max(settings.paper_min_prob_edge, base_edge)
    min_quality = settings.paper_observation_min_data_quality
    line_class = _line_class(candidate)
    is_line_market = family in LINE_MARKET_FAMILIES
    shadow_only = False
    line_policy = "normal"

    if line_class == "near_alternate":
        min_ev += Decimal("0.02")
        min_edge += Decimal("0.01")
        line_policy = "near_alternate_plus_0.02_ev_0.01_edge"
    elif line_class == "deep_alternate":
        min_ev += Decimal("0.04")
        min_edge += Decimal("0.02")
        line_policy = "deep_alternate_plus_0.04_ev_0.02_edge"
    elif line_class == "tail":
        min_ev = max(min_ev, Decimal("0.12"))
        min_edge = max(min_edge, Decimal("0.08"))
        line_policy = "tail_exceptional_or_shadow"
    elif line_class == "unclassified" and is_line_market:
        shadow_only = True
        line_policy = "unclassified_line_shadow_only"

    low_price = price is not None and price < settings.paper_low_price_threshold
    if low_price:
        min_ev = max(min_ev, settings.paper_low_price_min_net_ev)
        min_edge = max(min_edge, settings.paper_low_price_min_prob_edge)
        line_policy = f"{line_policy}+low_price_strict"

    profile = f"{family}:{line_class}:{'low_price' if low_price else 'standard'}"
    return SelectorThreshold(
        profile=profile,
        min_net_ev=_quantized(min_ev),
        min_prob_edge=_quantized(min_edge),
        min_data_quality=_quantized(min_quality, "0.0001"),
        line_class_policy=line_policy,
        shadow_only=shadow_only,
        line_class=line_class,
    )


def _rank_score(candidate: ModelCandidate) -> Decimal:
    ev = candidate.net_expected_value or Decimal("0")
    edge = candidate.probability_edge or Decimal("0")
    line_pref = Decimal(LINE_CLASS_PREFERENCE.get(_line_class(candidate), 0)) / Decimal("100")
    quality = candidate.data_quality or Decimal("0")
    return _quantized((ev * Decimal("100")) + (edge * Decimal("10")) + quality + line_pref)


def _rank_key(intent: SelectorIntent, settings: Settings) -> tuple[Decimal, Decimal, int, int, int]:
    candidate = intent.candidate
    not_low_price = 1 if intent.price >= settings.paper_low_price_threshold else 0
    candidate_id = candidate.id or 0
    return (
        candidate.net_expected_value or Decimal("0"),
        candidate.probability_edge or Decimal("0"),
        LINE_CLASS_PREFERENCE.get(_line_class(candidate), 0),
        not_low_price,
        -candidate_id,
    )


def selector_metadata_payload(candidate: ModelCandidate) -> dict[str, object]:
    return {
        "selector_policy_version": candidate.selector_policy_version,
        "selector_mode": candidate.selector_mode,
        "selector_status": candidate.selector_status,
        "selector_decision": candidate.selector_decision,
        "selector_rejection_reason": candidate.selector_rejection_reason,
        "selector_threshold_profile": candidate.selector_threshold_profile,
        "selector_min_net_ev": float(candidate.selector_min_net_ev) if candidate.selector_min_net_ev is not None else None,
        "selector_min_prob_edge": (
            float(candidate.selector_min_prob_edge) if candidate.selector_min_prob_edge is not None else None
        ),
        "selector_min_data_quality": (
            float(candidate.selector_min_data_quality) if candidate.selector_min_data_quality is not None else None
        ),
        "selector_line_class_policy": candidate.selector_line_class_policy,
        "selector_concept_cluster_key": candidate.selector_concept_cluster_key,
        "selector_same_game_concept_cluster_key": candidate.selector_same_game_concept_cluster_key,
        "selector_cluster_rank": candidate.selector_cluster_rank,
        "selector_cluster_rank_score": (
            float(candidate.selector_cluster_rank_score) if candidate.selector_cluster_rank_score is not None else None
        ),
        "selector_selected_from_cluster": candidate.selector_selected_from_cluster,
        "selector_shadow_only": candidate.selector_shadow_only,
        "selector_live_like_eligible_before_cluster": candidate.selector_live_like_eligible_before_cluster,
        "selector_live_like_eligible_after_cluster": candidate.selector_live_like_eligible_after_cluster,
    }


def _set_selector_gate(candidate: ModelCandidate, ok: bool) -> None:
    diagnostics = dict(candidate.gate_diagnostics or {})
    diagnostics["gate_live_like_selector_ok"] = ok
    if not ok:
        diagnostics["gate_caps_ok"] = False
        diagnostics["gate_final_trade_eligible"] = False
        candidate.gate_caps_ok = False
        candidate.gate_final_trade_eligible = False
    candidate.gate_diagnostics = diagnostics


def _apply_metadata(
    candidate: ModelCandidate,
    *,
    mode: str,
    status: str,
    decision: str,
    rejection_reason: str | None,
    threshold: SelectorThreshold,
    cluster_rank: int | None = None,
    cluster_rank_score: Decimal | None = None,
    selected_from_cluster: bool = False,
    live_like_eligible_before_cluster: bool = False,
    live_like_eligible_after_cluster: bool = False,
) -> None:
    candidate.selector_policy_version = SELECTOR_POLICY_VERSION
    candidate.selector_mode = mode
    candidate.selector_status = status
    candidate.selector_decision = decision
    candidate.selector_rejection_reason = rejection_reason
    candidate.selector_threshold_profile = threshold.profile
    candidate.selector_min_net_ev = threshold.min_net_ev
    candidate.selector_min_prob_edge = threshold.min_prob_edge
    candidate.selector_min_data_quality = threshold.min_data_quality
    candidate.selector_line_class_policy = threshold.line_class_policy
    candidate.selector_concept_cluster_key = candidate.concept_cluster_key
    candidate.selector_same_game_concept_cluster_key = candidate.same_game_concept_cluster_key
    candidate.selector_cluster_rank = cluster_rank
    candidate.selector_cluster_rank_score = cluster_rank_score
    candidate.selector_selected_from_cluster = selected_from_cluster
    candidate.selector_shadow_only = threshold.shadow_only or status == "shadow_only"
    candidate.selector_live_like_eligible_before_cluster = live_like_eligible_before_cluster
    candidate.selector_live_like_eligible_after_cluster = live_like_eligible_after_cluster
    diagnostics = dict(candidate.gate_diagnostics or {})
    diagnostics["selector"] = selector_metadata_payload(candidate)
    candidate.gate_diagnostics = diagnostics


def _threshold_rejection(candidate: ModelCandidate, threshold: SelectorThreshold) -> tuple[str, str, str] | None:
    if threshold.shadow_only:
        return "shadow_only", "no_trade_unclassified_line_shadow_only", "unclassified_line_shadow_only"
    if threshold.line_class == "tail":
        if (candidate.net_expected_value or Decimal("0")) < threshold.min_net_ev:
            return "shadow_only", "no_trade_tail_shadow_only", "tail_net_ev_below_exceptional_threshold"
        if (candidate.probability_edge or Decimal("0")) < threshold.min_prob_edge:
            return "shadow_only", "no_trade_tail_shadow_only", "tail_probability_edge_below_exceptional_threshold"
    if candidate.data_quality is None or candidate.data_quality < threshold.min_data_quality:
        return "rejected", "no_trade_selector_data_quality_threshold", "data_quality_below_selector_threshold"
    if candidate.net_expected_value is None or candidate.net_expected_value < threshold.min_net_ev:
        if threshold.line_class in {"near_alternate", "deep_alternate"}:
            return "rejected", "no_trade_selector_line_class_threshold", "line_class_net_ev_below_selector_threshold"
        return "rejected", "no_trade_selector_family_scope_threshold", "net_ev_below_selector_threshold"
    if candidate.probability_edge is None or candidate.probability_edge < threshold.min_prob_edge:
        if threshold.line_class in {"near_alternate", "deep_alternate"}:
            return (
                "rejected",
                "no_trade_selector_line_class_threshold",
                "line_class_probability_edge_below_selector_threshold",
            )
        return "rejected", "no_trade_selector_family_scope_threshold", "probability_edge_below_selector_threshold"
    return None


def _summary_template(mode: str) -> dict[str, object]:
    return {
        "selector_policy_version": SELECTOR_POLICY_VERSION,
        "selector_mode": mode,
        "selector_candidates_considered": 0,
        "selector_pre_cluster_eligible": 0,
        "selector_selected_after_cluster": 0,
        "selector_rejected_by_family_scope_threshold": 0,
        "selector_rejected_by_line_class": 0,
        "selector_rejected_by_concept_cluster": 0,
        "selector_shadow_only_count": 0,
        "selector_by_family_scope": {},
        "selector_by_line_class": {},
        "selector_by_concept_cluster_sample": [],
    }


def _increment_bucket(summary: dict[str, object], key: str, bucket: str, status: str) -> None:
    groups = summary.setdefault(key, {})
    if not isinstance(groups, dict):
        return
    counts = groups.setdefault(bucket, {"considered": 0, "selected": 0, "rejected": 0, "shadow_only": 0})
    if isinstance(counts, dict):
        counts["considered"] = int(counts.get("considered", 0)) + 1
        if status in counts:
            counts[status] = int(counts.get(status, 0)) + 1


def apply_live_like_selector(
    *,
    candidates: list[ModelCandidate],
    intents: list[SelectorIntent],
    settings: Settings,
    dry_run_candidates_only: bool = False,
) -> tuple[list[SelectorIntent], dict[str, int], dict[str, object]]:
    mode = settings.paper_selector_mode
    summary = _summary_template(mode)
    intent_candidate_ids = {intent.candidate.id for intent in intents if intent.candidate.id is not None}
    intent_by_id = {intent.candidate.id: intent for intent in intents if intent.candidate.id is not None}

    for candidate in candidates:
        intent = intent_by_id.get(candidate.id)
        threshold = selector_threshold(candidate, intent.price if intent is not None else candidate.executable_price, settings)
        _apply_metadata(
            candidate,
            mode=mode,
            status="not_considered" if candidate.id not in intent_candidate_ids else "pending",
            decision=candidate.decision,
            rejection_reason=None if candidate.id in intent_candidate_ids else "pre_selector_gate_failed",
            threshold=threshold,
        )

    if mode == "legacy":
        for intent in intents:
            threshold = selector_threshold(intent.candidate, intent.price, settings)
            _apply_metadata(
                intent.candidate,
                mode=mode,
                status="selected",
                decision="legacy_selected",
                rejection_reason=None,
                threshold=threshold,
                cluster_rank=1,
                cluster_rank_score=_rank_score(intent.candidate),
                selected_from_cluster=True,
                live_like_eligible_before_cluster=True,
                live_like_eligible_after_cluster=True,
            )
            _set_selector_gate(intent.candidate, True)
        summary["selector_candidates_considered"] = len(intents)
        summary["selector_pre_cluster_eligible"] = len(intents)
        summary["selector_selected_after_cluster"] = len(intents)
        return intents, {}, summary

    precluster: list[SelectorIntent] = []
    counts: dict[str, int] = {}
    for intent in intents:
        candidate = intent.candidate
        threshold = selector_threshold(candidate, intent.price, settings)
        summary["selector_candidates_considered"] = int(summary["selector_candidates_considered"]) + 1
        rejection = _threshold_rejection(candidate, threshold)
        if rejection is not None:
            status, decision, reason = rejection
            _apply_metadata(
                candidate,
                mode=mode,
                status=status,
                decision=decision,
                rejection_reason=reason,
                threshold=threshold,
                cluster_rank_score=_rank_score(candidate),
            )
            if not dry_run_candidates_only:
                candidate.decision = decision
                _set_selector_gate(candidate, False)
            if decision == "no_trade_selector_family_scope_threshold":
                summary["selector_rejected_by_family_scope_threshold"] = (
                    int(summary["selector_rejected_by_family_scope_threshold"]) + 1
                )
            if decision in {
                "no_trade_selector_line_class_threshold",
                "no_trade_tail_shadow_only",
                "no_trade_unclassified_line_shadow_only",
            }:
                summary["selector_rejected_by_line_class"] = int(summary["selector_rejected_by_line_class"]) + 1
            if status == "shadow_only":
                summary["selector_shadow_only_count"] = int(summary["selector_shadow_only_count"]) + 1
            counts[decision] = counts.get(decision, 0) + 1
            _increment_bucket(summary, "selector_by_family_scope", threshold.profile, "shadow_only" if status == "shadow_only" else "rejected")
            _increment_bucket(summary, "selector_by_line_class", threshold.line_class, "shadow_only" if status == "shadow_only" else "rejected")
            continue
        precluster.append(intent)
        _apply_metadata(
            candidate,
            mode=mode,
            status="pre_cluster_eligible",
            decision="pre_cluster_eligible",
            rejection_reason=None,
            threshold=threshold,
            cluster_rank_score=_rank_score(candidate),
            live_like_eligible_before_cluster=True,
        )

    summary["selector_pre_cluster_eligible"] = len(precluster)
    grouped: dict[str, list[SelectorIntent]] = {}
    for index, intent in enumerate(precluster):
        key = intent.candidate.same_game_concept_cluster_key or f"candidate:{intent.candidate.id or index}"
        grouped.setdefault(key, []).append(intent)

    selected: list[SelectorIntent] = []
    cluster_sample: list[dict[str, object]] = []
    for cluster_key, group in grouped.items():
        ranked = sorted(group, key=lambda item: _rank_key(item, settings), reverse=True)
        winner = ranked[0]
        selected.append(winner)
        if len(cluster_sample) < 10:
            cluster_sample.append(
                {
                    "same_game_concept_cluster_key": cluster_key,
                    "considered": len(ranked),
                    "selected_candidate_id": winner.candidate.id,
                    "rejected_count": max(len(ranked) - 1, 0),
                }
            )
        for rank, intent in enumerate(ranked, start=1):
            candidate = intent.candidate
            threshold = selector_threshold(candidate, intent.price, settings)
            rank_score = _rank_score(candidate)
            if rank == 1:
                _apply_metadata(
                    candidate,
                    mode=mode,
                    status="selected",
                    decision="selected_live_like",
                    rejection_reason=None,
                    threshold=threshold,
                    cluster_rank=rank,
                    cluster_rank_score=rank_score,
                    selected_from_cluster=True,
                    live_like_eligible_before_cluster=True,
                    live_like_eligible_after_cluster=True,
                )
                _set_selector_gate(candidate, True)
                _increment_bucket(summary, "selector_by_family_scope", threshold.profile, "selected")
                _increment_bucket(summary, "selector_by_line_class", threshold.line_class, "selected")
                continue
            _apply_metadata(
                candidate,
                mode=mode,
                status="rejected",
                decision="no_trade_selector_concept_cluster_not_best",
                rejection_reason="concept_cluster_not_best",
                threshold=threshold,
                cluster_rank=rank,
                cluster_rank_score=rank_score,
                selected_from_cluster=False,
                live_like_eligible_before_cluster=True,
                live_like_eligible_after_cluster=False,
            )
            if not dry_run_candidates_only:
                candidate.decision = "no_trade_selector_concept_cluster_not_best"
                _set_selector_gate(candidate, False)
            _increment_bucket(summary, "selector_by_family_scope", threshold.profile, "rejected")
            _increment_bucket(summary, "selector_by_line_class", threshold.line_class, "rejected")
            counts["no_trade_selector_concept_cluster_not_best"] = (
                counts.get("no_trade_selector_concept_cluster_not_best", 0) + 1
            )
            summary["selector_rejected_by_concept_cluster"] = int(summary["selector_rejected_by_concept_cluster"]) + 1

    summary["selector_selected_after_cluster"] = len(selected)
    summary["selector_by_concept_cluster_sample"] = cluster_sample
    return selected, counts, summary
