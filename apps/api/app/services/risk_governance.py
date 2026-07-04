from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import BalanceSnapshot, ModelCandidate, PaperTrade, PaperTradingEpoch
from app.services.contracts import (
    FIRST_FIVE_SPREAD,
    FIRST_FIVE_TOTAL,
    FIRST_FIVE_WINNER,
    FULL_GAME_SPREAD,
    FULL_GAME_TOTAL,
    FULL_GAME_WINNER,
)
from app.services.portfolio import calculate_paper_portfolio
from app.time_utils import ensure_aware_utc

RISK_GOVERNANCE_POLICY_VERSION = "pr3x_paper_risk_governance_v1"

RISK_GOVERNANCE_FIELD_NAMES: tuple[str, ...] = (
    "risk_governance_policy_version",
    "risk_governance_enabled",
    "risk_governance_status",
    "risk_governance_decision",
    "risk_governance_rejection_reason",
    "risk_governance_family_status",
    "risk_governance_family_cap_status",
    "risk_governance_concept_cluster_cap_status",
    "risk_governance_same_game_cap_status",
    "risk_governance_alternate_line_cap_status",
    "risk_governance_low_price_tail_cap_status",
    "risk_governance_drawdown_status",
    "risk_governance_approved_before_caps",
    "risk_governance_approved_after_caps",
    "risk_governance_shadow_only",
    "risk_governance_blocked",
    "risk_governance_rank",
    "risk_governance_rank_score",
)

LINE_MARKET_FAMILIES = {FULL_GAME_SPREAD, FIRST_FIVE_SPREAD, FULL_GAME_TOTAL, FIRST_FIVE_TOTAL}
ALTERNATE_LINE_FAMILIES = {FULL_GAME_TOTAL, FIRST_FIVE_TOTAL}


@dataclass(frozen=True)
class FamilyPolicy:
    status: str
    max_new_per_sweep: int
    max_open: int
    max_slate: int


@dataclass
class ExistingUsage:
    family_open: dict[str, int]
    family_slate: dict[str, int]
    concept_open: dict[str, int]
    game_open: dict[int, int]
    game_slate: dict[int, int]
    line_open: dict[str, int]
    line_slate: dict[str, int]
    low_price_open: int
    low_price_slate: int


class RiskIntent(Protocol):
    candidate: ModelCandidate
    game: Any
    market: Any
    price: Decimal
    score: Decimal


FAMILY_POLICIES: dict[str, FamilyPolicy] = {
    FULL_GAME_TOTAL: FamilyPolicy("enabled", 2, 3, 4),
    FIRST_FIVE_TOTAL: FamilyPolicy("enabled", 2, 3, 4),
    FULL_GAME_WINNER: FamilyPolicy("enabled", 2, 3, 4),
    FIRST_FIVE_WINNER: FamilyPolicy("enabled", 1, 2, 3),
    FULL_GAME_SPREAD: FamilyPolicy("paper_only", 1, 2, 3),
    FIRST_FIVE_SPREAD: FamilyPolicy("shadow_only", 1, 1, 2),
}

ALTERNATE_LINE_CAPS: dict[str, tuple[int, int, int]] = {
    "near_alternate": (2, 3, 4),
    "deep_alternate": (1, 2, 2),
    "tail": (1, 1, 1),
}


def risk_governance_policy_version(settings: Settings) -> str:
    return settings.paper_risk_governance_policy_version or RISK_GOVERNANCE_POLICY_VERSION


def _decimal(value: object, default: Decimal = Decimal("0")) -> Decimal:
    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return default


def _serializable(value: object) -> object:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return ensure_aware_utc(value).isoformat()
    return value


def _family_key(candidate: ModelCandidate) -> str:
    return str(candidate.market_family or "unknown")


def _line_class(candidate: ModelCandidate) -> str:
    return str(candidate.line_class or "not_applicable")


def _concept_key(candidate: ModelCandidate) -> str:
    return str(candidate.same_game_concept_cluster_key or candidate.concept_cluster_key or "")


def _game_id(candidate: ModelCandidate) -> int | None:
    return int(candidate.mlb_game_id) if candidate.mlb_game_id is not None else None


def _is_line_market(candidate: ModelCandidate) -> bool:
    return _family_key(candidate) in LINE_MARKET_FAMILIES


def _is_alternate_line_market(candidate: ModelCandidate) -> bool:
    return _family_key(candidate) in ALTERNATE_LINE_FAMILIES


def _is_low_price(intent: RiskIntent, settings: Settings) -> bool:
    return _decimal(intent.price) < settings.paper_low_price_threshold


def _family_policy(candidate: ModelCandidate) -> FamilyPolicy:
    return FAMILY_POLICIES.get(_family_key(candidate), FamilyPolicy("blocked", 0, 0, 0))


def _rank_score(intent: RiskIntent, settings: Settings) -> Decimal:
    candidate = intent.candidate
    selector_score = candidate.selector_cluster_rank_score
    if selector_score is not None:
        return _decimal(selector_score)
    line_preference = {
        "not_applicable": Decimal("5"),
        "central": Decimal("5"),
        "near_alternate": Decimal("4"),
        "deep_alternate": Decimal("3"),
        "tail": Decimal("2"),
        "unclassified": Decimal("1"),
    }.get(_line_class(candidate), Decimal("1"))
    low_price_penalty = Decimal("0.25") if _is_low_price(intent, settings) else Decimal("0")
    return (
        _decimal(candidate.net_expected_value) * Decimal("100")
        + _decimal(candidate.probability_edge) * Decimal("10")
        + line_preference
        - low_price_penalty
    )


def _rank_tuple(intent: RiskIntent, settings: Settings) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal, int]:
    candidate = intent.candidate
    score = _rank_score(intent, settings)
    selector_rank = _decimal(candidate.selector_cluster_rank_score, score)
    line_preference = {
        "not_applicable": Decimal("5"),
        "central": Decimal("5"),
        "near_alternate": Decimal("4"),
        "deep_alternate": Decimal("3"),
        "tail": Decimal("2"),
        "unclassified": Decimal("1"),
    }.get(_line_class(candidate), Decimal("1"))
    price_preference = Decimal("1") if not _is_low_price(intent, settings) else Decimal("0")
    stable_id = int(candidate.id or 0)
    return (
        selector_rank,
        _decimal(candidate.net_expected_value),
        _decimal(candidate.probability_edge),
        line_preference,
        price_preference,
        -stable_id,
    )


def _empty_payload(settings: Settings, *, status: str, decision: str, reason: str) -> dict[str, object]:
    return {
        "risk_governance_policy_version": risk_governance_policy_version(settings),
        "risk_governance_enabled": bool(settings.paper_risk_governance_enabled),
        "risk_governance_status": status,
        "risk_governance_decision": decision,
        "risk_governance_rejection_reason": reason,
        "risk_governance_family_status": "not_evaluated",
        "risk_governance_family_cap_status": "not_evaluated",
        "risk_governance_concept_cluster_cap_status": "not_evaluated",
        "risk_governance_same_game_cap_status": "not_evaluated",
        "risk_governance_alternate_line_cap_status": "not_evaluated",
        "risk_governance_low_price_tail_cap_status": "not_evaluated",
        "risk_governance_drawdown_status": "not_evaluated",
        "risk_governance_approved_before_caps": False,
        "risk_governance_approved_after_caps": False,
        "risk_governance_shadow_only": False,
        "risk_governance_blocked": False,
        "risk_governance_rank": 0,
        "risk_governance_rank_score": Decimal("0.000000"),
    }


def risk_governance_payload(candidate: ModelCandidate) -> dict[str, object]:
    return {name: _serializable(getattr(candidate, name, None)) for name in RISK_GOVERNANCE_FIELD_NAMES}


def risk_governance_field_counts(candidates: list[ModelCandidate]) -> dict[str, int]:
    return {name: sum(1 for candidate in candidates if getattr(candidate, name, None) is not None) for name in RISK_GOVERNANCE_FIELD_NAMES}


def _apply_payload(candidate: ModelCandidate, payload: dict[str, object], *, mutate_trade_gates: bool = True) -> None:
    for key in RISK_GOVERNANCE_FIELD_NAMES:
        if key in payload:
            setattr(candidate, key, payload[key])
    rationale = dict(candidate.scoring_rationale or {})
    rationale["risk_governance"] = risk_governance_payload(candidate)
    candidate.scoring_rationale = rationale
    diagnostics = dict(candidate.gate_diagnostics or {})
    diagnostics["risk_governance"] = risk_governance_payload(candidate)
    approved = bool(payload.get("risk_governance_approved_after_caps"))
    rejected = payload.get("risk_governance_status") in {"rejected", "shadow_only"}
    diagnostics["gate_risk_governance_ok"] = approved
    if mutate_trade_gates and not approved and rejected:
        diagnostics["gate_caps_ok"] = False
        diagnostics["gate_final_trade_eligible"] = False
        candidate.gate_caps_ok = False
        candidate.gate_final_trade_eligible = False
    candidate.gate_diagnostics = diagnostics


def copy_risk_governance_metadata_to_trade(trade: PaperTrade, candidate: ModelCandidate) -> None:
    for key in RISK_GOVERNANCE_FIELD_NAMES:
        if hasattr(trade, key):
            setattr(trade, key, getattr(candidate, key, None))


def drawdown_summary(session: Session, epoch: PaperTradingEpoch, settings: Settings) -> dict[str, object]:
    starting_balance = _decimal(epoch.starting_balance, settings.paper_bankroll_starting_balance)
    max_snapshot_value = session.scalar(
        select(func.max(BalanceSnapshot.portfolio_value)).where(BalanceSnapshot.paper_trading_epoch_id == epoch.id)
    )
    current = calculate_paper_portfolio(session, epoch=epoch)
    current_value = _decimal(current.portfolio_value, starting_balance)
    high_water = max(starting_balance, _decimal(max_snapshot_value, starting_balance), current_value)
    drawdown_abs = max(high_water - current_value, Decimal("0"))
    drawdown_pct = Decimal("0") if high_water <= 0 else drawdown_abs / high_water
    halted = bool(
        settings.paper_drawdown_halt_enabled
        and (
            drawdown_abs >= settings.paper_drawdown_halt_threshold_abs
            or drawdown_pct >= settings.paper_drawdown_halt_threshold_pct
        )
    )
    return {
        "enabled": bool(settings.paper_drawdown_halt_enabled),
        "status": "halted" if halted else "clear",
        "starting_balance": float(starting_balance),
        "high_water_mark": float(high_water),
        "current_portfolio_value": float(current_value),
        "drawdown_abs": float(drawdown_abs),
        "drawdown_pct": float(drawdown_pct),
        "threshold_abs": float(settings.paper_drawdown_halt_threshold_abs),
        "threshold_pct": float(settings.paper_drawdown_halt_threshold_pct),
    }


def _bump(counter: dict[str, int], key: str, amount: int = 1) -> None:
    counter[key] = counter.get(key, 0) + amount


def _existing_usage(
    session: Session,
    *,
    epoch_id: int | None,
    target_date: date,
    day_start: datetime,
    day_end: datetime,
    settings: Settings,
) -> ExistingUsage:
    usage = ExistingUsage({}, {}, {}, {}, {}, {}, {}, 0, 0)
    stmt = (
        select(PaperTrade, ModelCandidate)
        .outerjoin(ModelCandidate, PaperTrade.candidate_id == ModelCandidate.id)
        .where(PaperTrade.status == "open")
    )
    if epoch_id is not None:
        stmt = stmt.where(PaperTrade.paper_trading_epoch_id == epoch_id)
    for trade, candidate in session.execute(stmt):
        family = str(
            (candidate.market_family if candidate is not None else None)
            or trade.market_family
            or "unknown"
        )
        concept = str(
            (candidate.same_game_concept_cluster_key if candidate is not None else None)
            or (candidate.concept_cluster_key if candidate is not None else None)
            or trade.same_game_concept_cluster_key
            or trade.concept_cluster_key
            or ""
        )
        line_class = str((candidate.line_class if candidate is not None else None) or trade.line_class or "not_applicable")
        game_id = int(candidate.mlb_game_id) if candidate is not None and candidate.mlb_game_id is not None else None
        entry_time = ensure_aware_utc(trade.entry_time)
        same_slate = (
            bool(candidate is not None and candidate.target_date == target_date)
            or ensure_aware_utc(day_start) <= entry_time < ensure_aware_utc(day_end)
        )
        _bump(usage.family_open, family)
        _bump(usage.line_open, line_class)
        if concept:
            _bump(usage.concept_open, concept)
        if game_id is not None:
            _bump(usage.game_open, game_id)
        if _decimal(trade.entry_price) < settings.paper_low_price_threshold:
            usage.low_price_open += 1
        if same_slate:
            _bump(usage.family_slate, family)
            _bump(usage.line_slate, line_class)
            if game_id is not None:
                _bump(usage.game_slate, game_id)
            if _decimal(trade.entry_price) < settings.paper_low_price_threshold:
                usage.low_price_slate += 1
    return usage


def _count_payload(summary: dict[str, object], candidate: ModelCandidate, payload: dict[str, object]) -> None:
    family = _family_key(candidate)
    line_class = _line_class(candidate)
    by_family = summary.setdefault("risk_by_family_scope", {})
    if isinstance(by_family, dict):
        bucket = by_family.setdefault(family, {"considered": 0, "approved": 0, "rejected": 0})
        if isinstance(bucket, dict):
            bucket["considered"] = int(bucket.get("considered", 0)) + 1
            if payload.get("risk_governance_approved_after_caps"):
                bucket["approved"] = int(bucket.get("approved", 0)) + 1
            else:
                bucket["rejected"] = int(bucket.get("rejected", 0)) + 1
    by_line = summary.setdefault("risk_by_line_class", {})
    if isinstance(by_line, dict):
        bucket = by_line.setdefault(line_class, {"considered": 0, "approved": 0, "rejected": 0})
        if isinstance(bucket, dict):
            bucket["considered"] = int(bucket.get("considered", 0)) + 1
            if payload.get("risk_governance_approved_after_caps"):
                bucket["approved"] = int(bucket.get("approved", 0)) + 1
            else:
                bucket["rejected"] = int(bucket.get("rejected", 0)) + 1
    game_id = _game_id(candidate)
    if game_id is not None:
        sample = summary.setdefault("risk_by_same_game_sample", {})
        if isinstance(sample, dict) and len(sample) < 10:
            key = str(game_id)
            bucket = sample.setdefault(key, {"considered": 0, "approved": 0, "rejected": 0})
            if isinstance(bucket, dict):
                bucket["considered"] = int(bucket.get("considered", 0)) + 1
                if payload.get("risk_governance_approved_after_caps"):
                    bucket["approved"] = int(bucket.get("approved", 0)) + 1
                else:
                    bucket["rejected"] = int(bucket.get("rejected", 0)) + 1


def apply_risk_governance(
    session: Session,
    *,
    candidates: list[ModelCandidate],
    intents: list[RiskIntent],
    settings: Settings,
    active_epoch: PaperTradingEpoch,
    target_date: date,
    day_start: datetime,
    day_end: datetime,
    dry_run_candidates_only: bool = False,
) -> tuple[list[RiskIntent], dict[str, int], dict[str, object]]:
    drawdown = drawdown_summary(session, active_epoch, settings)
    counts: dict[str, int] = {}
    summary: dict[str, object] = {
        "risk_governance_policy_version": risk_governance_policy_version(settings),
        "risk_governance_enabled": bool(settings.paper_risk_governance_enabled),
        "risk_candidates_considered": len(intents),
        "risk_approved_before_caps": 0,
        "risk_approved_after_caps": 0,
        "risk_shadow_only_count": 0,
        "risk_blocked_count": 0,
        "risk_rejected_by_family_status": 0,
        "risk_rejected_by_family_cap": 0,
        "risk_rejected_by_concept_cluster_cap": 0,
        "risk_rejected_by_same_game_cap": 0,
        "risk_rejected_by_alternate_line_cap": 0,
        "risk_rejected_by_low_price_tail_cap": 0,
        "risk_rejected_by_drawdown_halt": 0,
        "risk_by_family_scope": {},
        "risk_by_line_class": {},
        "risk_by_same_game_sample": {},
        "risk_drawdown_summary": drawdown,
    }
    if not settings.paper_risk_governance_enabled:
        selected = list(intents)
        intent_ids = {id(intent.candidate) for intent in intents}
        for candidate in candidates:
            payload = _empty_payload(settings, status="disabled", decision="disabled", reason="risk_governance_disabled")
            if id(candidate) in intent_ids:
                payload.update(
                    {
                        "risk_governance_status": "approved",
                        "risk_governance_decision": "approved_disabled_policy",
                        "risk_governance_rejection_reason": "none",
                        "risk_governance_approved_before_caps": True,
                        "risk_governance_approved_after_caps": True,
                    }
                )
            _apply_payload(candidate, payload, mutate_trade_gates=not dry_run_candidates_only)
        summary["risk_approved_before_caps"] = len(intents)
        summary["risk_approved_after_caps"] = len(selected)
        return selected, counts, summary

    selected: list[RiskIntent] = []
    usage = _existing_usage(
        session,
        epoch_id=active_epoch.id,
        target_date=target_date,
        day_start=day_start,
        day_end=day_end,
        settings=settings,
    )
    selected_family: dict[str, int] = {}
    selected_concept: dict[str, int] = {}
    selected_game: dict[int, int] = {}
    selected_line: dict[str, int] = {}
    selected_low_price = 0
    intent_candidates = {id(intent.candidate) for intent in intents}
    for candidate in candidates:
        payload = _empty_payload(
            settings,
            status="not_considered",
            decision="not_considered",
            reason="pre_risk_gate_not_selected",
        )
        policy = _family_policy(candidate)
        payload["risk_governance_family_status"] = policy.status
        if id(candidate) not in intent_candidates:
            _apply_payload(candidate, payload, mutate_trade_gates=False)

    ranked_intents = sorted(intents, key=lambda intent: _rank_tuple(intent, settings), reverse=True)
    for rank, intent in enumerate(ranked_intents, start=1):
        candidate = intent.candidate
        family = _family_key(candidate)
        policy = _family_policy(candidate)
        line_class = _line_class(candidate)
        concept = _concept_key(candidate)
        game_id = _game_id(candidate)
        low_price = _is_low_price(intent, settings)
        payload = _empty_payload(settings, status="rejected", decision="rejected", reason="none")
        payload.update(
            {
                "risk_governance_family_status": policy.status,
                "risk_governance_family_cap_status": "passed",
                "risk_governance_concept_cluster_cap_status": "passed" if concept else "not_applicable",
                "risk_governance_same_game_cap_status": "passed" if game_id is not None else "not_applicable",
                "risk_governance_alternate_line_cap_status": "not_applicable",
                "risk_governance_low_price_tail_cap_status": "passed" if low_price else "not_applicable",
                "risk_governance_drawdown_status": str(drawdown["status"]),
                "risk_governance_rank": rank,
                "risk_governance_rank_score": _rank_score(intent, settings).quantize(Decimal("0.000001")),
            }
        )

        def reject(decision: str, reason: str, count_key: str, field: str, field_status: str) -> None:
            payload.update(
                {
                    "risk_governance_status": "rejected",
                    "risk_governance_decision": decision,
                    "risk_governance_rejection_reason": reason,
                    "risk_governance_approved_before_caps": bool(
                        decision
                        not in {
                            "rejected_by_family_status",
                            "rejected_by_drawdown_halt",
                            "rejected_by_probability_hardening",
                        }
                    ),
                    "risk_governance_approved_after_caps": False,
                    "risk_governance_blocked": True,
                    field: field_status,
                }
            )
            _bump(counts, reason)
            summary[count_key] = int(summary.get(count_key, 0)) + 1

        if policy.status == "shadow_only":
            payload.update(
                {
                    "risk_governance_status": "shadow_only",
                    "risk_governance_decision": "rejected_by_family_status",
                    "risk_governance_rejection_reason": "no_trade_risk_family_shadow_only",
                    "risk_governance_family_cap_status": "shadow_only",
                    "risk_governance_shadow_only": True,
                    "risk_governance_approved_before_caps": False,
                    "risk_governance_approved_after_caps": False,
                    "risk_governance_blocked": True,
                }
            )
            _bump(counts, "no_trade_risk_family_shadow_only")
            summary["risk_rejected_by_family_status"] = int(summary["risk_rejected_by_family_status"]) + 1
            summary["risk_shadow_only_count"] = int(summary["risk_shadow_only_count"]) + 1
        elif policy.status == "blocked":
            reject(
                "rejected_by_family_status",
                "no_trade_risk_family_blocked",
                "risk_rejected_by_family_status",
                "risk_governance_family_cap_status",
                "blocked",
            )
        elif drawdown["status"] == "halted":
            reject(
                "rejected_by_drawdown_halt",
                "no_trade_risk_drawdown_halt",
                "risk_rejected_by_drawdown_halt",
                "risk_governance_drawdown_status",
                "halted",
            )
        elif candidate.probability_hardening_block_recommendation or candidate.probability_hardening_shadow_only:
            reject(
                "rejected_by_probability_hardening",
                "no_trade_risk_probability_hardening_block",
                "risk_rejected_by_alternate_line_cap",
                "risk_governance_alternate_line_cap_status",
                "blocked_by_probability_hardening",
            )
        else:
            payload["risk_governance_approved_before_caps"] = True
            max_new = min(policy.max_new_per_sweep, int(settings.paper_max_new_trades_per_sweep))
            max_open = min(policy.max_open, int(settings.paper_max_open_positions))
            max_slate = min(policy.max_slate, int(settings.paper_max_trades_per_market_family))
            if selected_family.get(family, 0) >= max_new:
                reject(
                    "rejected_by_family_cap",
                    "no_trade_risk_family_new_cap",
                    "risk_rejected_by_family_cap",
                    "risk_governance_family_cap_status",
                    "new_cap_reached",
                )
            elif usage.family_open.get(family, 0) + selected_family.get(family, 0) >= max_open:
                reject(
                    "rejected_by_family_cap",
                    "no_trade_risk_family_open_cap",
                    "risk_rejected_by_family_cap",
                    "risk_governance_family_cap_status",
                    "open_cap_reached",
                )
            elif usage.family_slate.get(family, 0) + selected_family.get(family, 0) >= max_slate:
                reject(
                    "rejected_by_family_cap",
                    "no_trade_risk_family_slate_cap",
                    "risk_rejected_by_family_cap",
                    "risk_governance_family_cap_status",
                    "slate_cap_reached",
                )
            elif concept and (usage.concept_open.get(concept, 0) > 0 or selected_concept.get(concept, 0) > 0):
                reject(
                    "rejected_by_concept_cluster_cap",
                    "no_trade_risk_concept_cluster_cap",
                    "risk_rejected_by_concept_cluster_cap",
                    "risk_governance_concept_cluster_cap_status",
                    "concept_cap_reached",
                )
            elif game_id is not None and selected_game.get(game_id, 0) >= 1:
                reject(
                    "rejected_by_same_game_cap",
                    "no_trade_risk_same_game_new_cap",
                    "risk_rejected_by_same_game_cap",
                    "risk_governance_same_game_cap_status",
                    "new_cap_reached",
                )
            elif game_id is not None and usage.game_open.get(game_id, 0) >= 2:
                reject(
                    "rejected_by_same_game_cap",
                    "no_trade_risk_same_game_open_cap",
                    "risk_rejected_by_same_game_cap",
                    "risk_governance_same_game_cap_status",
                    "open_cap_reached",
                )
            elif game_id is not None and usage.game_slate.get(game_id, 0) + selected_game.get(game_id, 0) >= 2:
                reject(
                    "rejected_by_same_game_cap",
                    "no_trade_risk_same_game_slate_cap",
                    "risk_rejected_by_same_game_cap",
                    "risk_governance_same_game_cap_status",
                    "slate_cap_reached",
                )
            elif _is_alternate_line_market(candidate) and line_class == "unclassified":
                reject(
                    "rejected_by_alternate_line_cap",
                    "no_trade_risk_line_unclassified",
                    "risk_rejected_by_alternate_line_cap",
                    "risk_governance_alternate_line_cap_status",
                    "unclassified_blocked",
                )
            elif line_class in ALTERNATE_LINE_CAPS and selected_line.get(line_class, 0) >= ALTERNATE_LINE_CAPS[line_class][0]:
                reject(
                    "rejected_by_alternate_line_cap",
                    "no_trade_risk_alternate_line_new_cap",
                    "risk_rejected_by_alternate_line_cap",
                    "risk_governance_alternate_line_cap_status",
                    "new_cap_reached",
                )
            elif line_class in ALTERNATE_LINE_CAPS and usage.line_open.get(line_class, 0) >= ALTERNATE_LINE_CAPS[line_class][1]:
                reject(
                    "rejected_by_alternate_line_cap",
                    "no_trade_risk_alternate_line_open_cap",
                    "risk_rejected_by_alternate_line_cap",
                    "risk_governance_alternate_line_cap_status",
                    "open_cap_reached",
                )
            elif line_class in ALTERNATE_LINE_CAPS and usage.line_slate.get(line_class, 0) + selected_line.get(line_class, 0) >= ALTERNATE_LINE_CAPS[line_class][2]:
                reject(
                    "rejected_by_alternate_line_cap",
                    "no_trade_risk_alternate_line_slate_cap",
                    "risk_rejected_by_alternate_line_cap",
                    "risk_governance_alternate_line_cap_status",
                    "slate_cap_reached",
                )
            elif low_price and line_class == "tail":
                reject(
                    "rejected_by_low_price_tail_cap",
                    "no_trade_risk_low_price_tail_blocked",
                    "risk_rejected_by_low_price_tail_cap",
                    "risk_governance_low_price_tail_cap_status",
                    "low_price_tail_blocked",
                )
            elif low_price and selected_low_price >= min(int(settings.paper_low_price_max_trades_per_sweep), 1):
                reject(
                    "rejected_by_low_price_tail_cap",
                    "no_trade_risk_low_price_new_cap",
                    "risk_rejected_by_low_price_tail_cap",
                    "risk_governance_low_price_tail_cap_status",
                    "new_cap_reached",
                )
            elif low_price and usage.low_price_open >= 2:
                reject(
                    "rejected_by_low_price_tail_cap",
                    "no_trade_risk_low_price_open_cap",
                    "risk_rejected_by_low_price_tail_cap",
                    "risk_governance_low_price_tail_cap_status",
                    "open_cap_reached",
                )
            elif low_price and usage.low_price_slate + selected_low_price >= min(int(settings.paper_low_price_max_trades_per_slate), 2):
                reject(
                    "rejected_by_low_price_tail_cap",
                    "no_trade_risk_low_price_slate_cap",
                    "risk_rejected_by_low_price_tail_cap",
                    "risk_governance_low_price_tail_cap_status",
                    "slate_cap_reached",
                )
            else:
                payload.update(
                    {
                        "risk_governance_status": "approved",
                        "risk_governance_decision": "approved_for_paper_trade",
                        "risk_governance_rejection_reason": "none",
                        "risk_governance_approved_after_caps": True,
                    }
                )
                selected.append(intent)
                _bump(selected_family, family)
                _bump(selected_line, line_class)
                if concept:
                    _bump(selected_concept, concept)
                if game_id is not None:
                    _bump(selected_game, game_id)
                if low_price:
                    selected_low_price += 1
                summary["risk_approved_after_caps"] = int(summary["risk_approved_after_caps"]) + 1

        if payload.get("risk_governance_approved_before_caps"):
            summary["risk_approved_before_caps"] = int(summary["risk_approved_before_caps"]) + 1
        if payload.get("risk_governance_blocked"):
            summary["risk_blocked_count"] = int(summary["risk_blocked_count"]) + 1
        _apply_payload(candidate, payload, mutate_trade_gates=not dry_run_candidates_only)
        if payload.get("risk_governance_blocked") and not dry_run_candidates_only:
            candidate.decision = str(payload["risk_governance_rejection_reason"])
        _count_payload(summary, candidate, payload)

    return selected, counts, summary
