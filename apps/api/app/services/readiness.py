from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    BalanceSnapshot,
    JobRun,
    ModelCandidate,
    ModelPredictionRun,
    PaperTrade,
    PaperTradingEpoch,
    Settlement,
)
from app.services.exposure_taxonomy import EXPOSURE_TAXONOMY_VERSION, LINE_CLASSIFICATION_POLICY_VERSION
from app.services.live_like_selector import SELECTOR_POLICY_VERSION
from app.services.modeling import FAMILY_SCOPE_GOVERNANCE_POLICY, governance_status
from app.services.portfolio import calculate_paper_portfolio
from app.services.probability_adapters import PROBABILITY_ADAPTER_POLICY_VERSION
from app.services.probability_hardening import PROBABILITY_HARDENING_POLICY_VERSION
from app.services.risk_governance import (
    DRAWDOWN_LIVE_HALT_THRESHOLD_ABS,
    DRAWDOWN_POLICY_VERSION,
    drawdown_summary,
    risk_governance_policy_version,
)
from app.services.settlement import SETTLEMENT_FORMULA_VERSION
from app.time_utils import to_eastern_iso, utc_now

READINESS_POLICY_VERSION = "pr4b_final_readiness_audit_pack_v1"
LIVE_READINESS_STATUS = "blocked_for_live"
PAPER_OBSERVATION_READY = "paper_observation_ready"
PAPER_OBSERVATION_BLOCKED = "paper_observation_blocked"
UNKNOWN_EVIDENCE = "unknown_due_missing_evidence"

TAXONOMY_FIELDS = (
    "economic_exposure_label",
    "economic_exposure_key",
    "economic_exposure_family",
    "economic_exposure_scope",
    "economic_exposure_direction",
    "contract_mechanics_label",
    "concept_cluster_key",
    "same_game_concept_cluster_key",
    "line_class",
    "line_class_reason",
    "exposure_taxonomy_version",
    "line_classification_policy_version",
)
SELECTOR_FIELDS = (
    "selector_policy_version",
    "selector_mode",
    "selector_status",
    "selector_decision",
    "selector_threshold_profile",
    "selector_min_net_ev",
    "selector_min_prob_edge",
    "selector_min_data_quality",
    "selector_line_class_policy",
    "selector_live_like_eligible_before_cluster",
    "selector_live_like_eligible_after_cluster",
)
ADAPTER_FIELDS = (
    "probability_adapter_key",
    "probability_adapter_version",
    "probability_adapter_policy_version",
    "probability_adapter_family",
    "probability_adapter_scope",
    "probability_adapter_calibration_hook",
    "probability_adapter_calibration_version",
    "probability_adapter_feature_policy_version",
)
HARDENING_FIELDS = (
    "probability_hardening_policy_version",
    "probability_hardening_enabled",
    "probability_raw_adapter",
    "probability_before_hardening",
    "probability_after_hardening",
    "probability_hardening_status",
    "probability_hardening_line_class",
    "probability_hardening_line_class_policy",
)
RISK_FIELDS = (
    "risk_governance_policy_version",
    "risk_governance_enabled",
    "risk_governance_status",
    "risk_governance_decision",
    "risk_governance_rejection_reason",
    "risk_governance_family_status",
    "risk_governance_drawdown_status",
    "risk_governance_approved_before_caps",
    "risk_governance_approved_after_caps",
    "risk_governance_blocked",
)


def _iso(value: datetime | None) -> str | None:
    return to_eastern_iso(value) if value is not None else None


def _float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _status(ok: bool) -> str:
    return "pass" if ok else "fail"


def _active_epoch(session: Session | None) -> PaperTradingEpoch | None:
    if session is None:
        return None
    return session.scalar(
        select(PaperTradingEpoch)
        .where(PaperTradingEpoch.status == "active")
        .order_by(PaperTradingEpoch.started_at.desc(), PaperTradingEpoch.id.desc())
        .limit(1)
    )


def _latest_job(session: Session, epoch_id: int, job_name: str) -> dict[str, object]:
    row = session.execute(
        select(
            JobRun.id,
            JobRun.status,
            JobRun.started_at,
            JobRun.completed_at,
            JobRun.duration_seconds,
            JobRun.target_date,
        )
        .where(JobRun.paper_trading_epoch_id == epoch_id)
        .where(JobRun.job_name == job_name)
        .order_by(JobRun.started_at.desc(), JobRun.id.desc())
        .limit(1)
    ).first()
    if row is None:
        return {"status": "not_run"}
    return {
        "job_run_id": row.id,
        "status": row.status,
        "started_at": _iso(row.started_at),
        "completed_at": _iso(row.completed_at),
        "duration_seconds": row.duration_seconds,
        "target_date": row.target_date.isoformat() if row.target_date else None,
    }


def _count(session: Session, statement) -> int:
    return int(session.scalar(statement) or 0)


def _field_complete_count(session: Session, epoch_id: int, fields: Iterable[str]) -> int:
    predicates = [getattr(ModelCandidate, field).is_not(None) for field in fields]
    return _count(
        session,
        select(func.count(ModelCandidate.id))
        .where(ModelCandidate.paper_trading_epoch_id == epoch_id)
        .where(and_(*predicates)),
    )


def _field_counts(session: Session, epoch_id: int, fields: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for field in fields:
        counts[field] = _count(
            session,
            select(func.count(ModelCandidate.id))
            .where(ModelCandidate.paper_trading_epoch_id == epoch_id)
            .where(getattr(ModelCandidate, field).is_not(None)),
        )
    return counts


def _status_from_count(count: int) -> str:
    return "available" if count > 0 else UNKNOWN_EVIDENCE


def _candidate_pipeline(session: Session | None, epoch: PaperTradingEpoch | None) -> dict[str, object]:
    settings = get_settings()
    base: dict[str, object] = {
        "feature_sync_mode": "cache_only",
        "feature_sync_skipped": True,
        "heavy_feature_sync_skipped": True,
        "selector_policy_version": SELECTOR_POLICY_VERSION,
        "selector_mode": settings.paper_selector_mode,
        "probability_adapter_policy_version": PROBABILITY_ADAPTER_POLICY_VERSION,
        "probability_hardening_policy_version": settings.paper_probability_hardening_policy_version
        or PROBABILITY_HARDENING_POLICY_VERSION,
        "risk_governance_policy_version": risk_governance_policy_version(settings),
        "risk_governance_enabled": settings.paper_risk_governance_enabled,
        "exposure_taxonomy_version": EXPOSURE_TAXONOMY_VERSION,
        "line_classification_policy_version": LINE_CLASSIFICATION_POLICY_VERSION,
    }
    if session is None or epoch is None:
        return {**base, "status": UNKNOWN_EVIDENCE, "latest_candidate_sweep": {"status": "not_run"}}

    latest_prediction = session.execute(
        select(
            ModelPredictionRun.id,
            ModelPredictionRun.status,
            ModelPredictionRun.started_at,
            ModelPredictionRun.completed_at,
            ModelPredictionRun.target_date,
            ModelPredictionRun.candidates_evaluated,
            ModelPredictionRun.trades_created,
            ModelPredictionRun.feature_version,
        )
        .where(ModelPredictionRun.paper_trading_epoch_id == epoch.id)
        .order_by(ModelPredictionRun.started_at.desc(), ModelPredictionRun.id.desc())
        .limit(1)
    ).first()
    candidate_count = _count(
        session,
        select(func.count(ModelCandidate.id)).where(ModelCandidate.paper_trading_epoch_id == epoch.id),
    )
    line_class_rows = session.execute(
        select(ModelCandidate.line_class, func.count(ModelCandidate.id))
        .where(ModelCandidate.paper_trading_epoch_id == epoch.id)
        .group_by(ModelCandidate.line_class)
    )
    line_classification_counts = {
        str(line_class or "missing"): int(count or 0) for line_class, count in line_class_rows
    }
    pipeline = {
        **base,
        "status": "available" if latest_prediction or candidate_count else UNKNOWN_EVIDENCE,
        "latest_candidate_sweep": _latest_job(session, epoch.id, "candidate-sweep"),
        "candidate_rows": candidate_count,
        "taxonomy_complete_count": _field_complete_count(session, epoch.id, TAXONOMY_FIELDS),
        "selector_complete_count": _field_complete_count(session, epoch.id, SELECTOR_FIELDS),
        "probability_adapter_complete_count": _field_complete_count(session, epoch.id, ADAPTER_FIELDS),
        "probability_hardening_core_count": _field_complete_count(session, epoch.id, HARDENING_FIELDS),
        "risk_governance_core_count": _field_complete_count(session, epoch.id, RISK_FIELDS),
        "candidate_exposure_field_counts": _field_counts(session, epoch.id, TAXONOMY_FIELDS),
        "candidate_selector_field_counts": _field_counts(session, epoch.id, SELECTOR_FIELDS),
        "candidate_probability_adapter_field_counts": _field_counts(session, epoch.id, ADAPTER_FIELDS),
        "candidate_probability_hardening_field_counts": _field_counts(session, epoch.id, HARDENING_FIELDS),
        "candidate_risk_governance_field_counts": _field_counts(session, epoch.id, RISK_FIELDS),
        "line_classification_counts": line_classification_counts,
    }
    if latest_prediction is not None:
        pipeline["latest_prediction_run"] = {
            "prediction_run_id": latest_prediction.id,
            "status": latest_prediction.status,
            "started_at": _iso(latest_prediction.started_at),
            "completed_at": _iso(latest_prediction.completed_at),
            "target_date": latest_prediction.target_date.isoformat() if latest_prediction.target_date else None,
            "candidates_evaluated": latest_prediction.candidates_evaluated,
            "trades_created": latest_prediction.trades_created,
            "feature_version": latest_prediction.feature_version,
        }
    return pipeline


def _accounting(session: Session | None, epoch: PaperTradingEpoch | None) -> dict[str, object]:
    if session is None or epoch is None:
        return {"status": UNKNOWN_EVIDENCE}
    totals = calculate_paper_portfolio(session, epoch=epoch)
    snapshot_count = _count(
        session,
        select(func.count(BalanceSnapshot.id)).where(BalanceSnapshot.paper_trading_epoch_id == epoch.id),
    )
    drawdown = drawdown_summary(session, epoch, get_settings())
    return {
        "status": "available",
        "active_epoch_id": epoch.id,
        "active_epoch_key": epoch.epoch_key,
        "starting_balance": float(totals.starting_balance),
        "cash_balance": float(totals.cash_balance),
        "portfolio_value": float(totals.portfolio_value),
        "current_equity": float(totals.current_equity),
        "open_cost": float(totals.open_cost),
        "open_mark_value": float(totals.open_mark_value),
        "open_unrealized_pnl": float(totals.open_unrealized_pnl),
        "realized_pnl": float(totals.realized_pnl),
        "open_fees_estimated": float(totals.open_fees_estimated),
        "settled_fees_paid": float(totals.settled_fees_paid),
        "open_trade_count": totals.open_trade_count,
        "settled_trade_count": totals.settled_trade_count,
        "balance_snapshot_count": snapshot_count,
        "drawdown_policy_version": drawdown.get("drawdown_policy_version") or DRAWDOWN_POLICY_VERSION,
        "drawdown_observation_mode": bool(drawdown.get("drawdown_observation_mode")),
        "drawdown_halt_enforced": bool(drawdown.get("drawdown_halt_enforced")),
        "drawdown_would_have_halted": bool(drawdown.get("drawdown_would_have_halted")),
        "drawdown_live_halt_threshold_abs": float(DRAWDOWN_LIVE_HALT_THRESHOLD_ABS),
        "drawdown_live_halt_basis": "starting_bankroll_minus_150",
        "drawdown_status": drawdown.get("status"),
    }


def _model_governance(session: Session | None, epoch: PaperTradingEpoch | None) -> dict[str, object]:
    if session is None or epoch is None:
        return {
            "status": UNKNOWN_EVIDENCE,
            "governance_policy_version": FAMILY_SCOPE_GOVERNANCE_POLICY,
        }
    summary = governance_status(session, epoch.id, include_details=False)
    return {
        "status": str(summary.get("last_governance_status") or "not_run"),
        "governance_policy_version": summary.get("governance_policy_version") or FAMILY_SCOPE_GOVERNANCE_POLICY,
        "governance_training_policy": summary.get("governance_training_policy"),
        "active_model_version": summary.get("active_model_version"),
        "active_parameter_version": summary.get("active_parameter_version"),
        "active_calibration_version": summary.get("active_calibration_version"),
        "feature_version": summary.get("feature_version"),
        "raw_resolved_mature_samples": summary.get("raw_resolved_mature_samples"),
        "clean_resolved_mature_samples": summary.get("clean_resolved_mature_samples"),
        "pre_clean_excluded_samples": summary.get("pre_clean_excluded_samples"),
        "clean_training_eligible_count": summary.get("clean_training_eligible_count"),
        "family_scope_governance_enabled": bool(summary.get("family_scope_governance_enabled")),
        "family_scope_unit_count": summary.get("family_scope_unit_count"),
        "adapter_error_count": summary.get("adapter_error_count", 0),
        "adapter_errors_excluded_from_training": summary.get("adapter_errors_excluded_from_training", True),
        "last_training_run": summary.get("last_training_run"),
        "last_calibration_run": summary.get("last_calibration_run"),
    }


def _settlement_audit(session: Session | None, epoch: PaperTradingEpoch | None) -> dict[str, object]:
    if session is None or epoch is None:
        return {
            "status": UNKNOWN_EVIDENCE,
            "settlement_formula_version": SETTLEMENT_FORMULA_VERSION,
        }
    trade_filter = PaperTrade.paper_trading_epoch_id == epoch.id
    settled_or_checked = PaperTrade.status.in_(("settled", "closed", "void")) | PaperTrade.settlement_checked_at.is_not(None)
    checked_count = _count(session, select(func.count(PaperTrade.id)).where(trade_filter).where(settled_or_checked))
    audit_count = _count(
        session,
        select(func.count(PaperTrade.id))
        .where(trade_filter)
        .where(PaperTrade.settlement_audit_key.is_not(None))
        .where(PaperTrade.settlement_formula_version.is_not(None))
        .where(PaperTrade.settlement_idempotency_key.is_not(None)),
    )
    missing_audit_count = _count(
        session,
        select(func.count(PaperTrade.id))
        .where(trade_filter)
        .where(settled_or_checked)
        .where(
            (PaperTrade.settlement_audit_key.is_(None))
            | (PaperTrade.settlement_formula_version.is_(None))
            | (PaperTrade.settlement_idempotency_key.is_(None))
        ),
    )
    settlement_rows = _count(
        session,
        select(func.count(Settlement.id)).join(PaperTrade, Settlement.paper_trade_id == PaperTrade.id).where(trade_filter),
    )
    return {
        "status": "available" if audit_count else UNKNOWN_EVIDENCE,
        "latest_settlement_job": _latest_job(session, epoch.id, "settlement"),
        "settlement_formula_version": SETTLEMENT_FORMULA_VERSION,
        "checked_or_terminal_trade_count": checked_count,
        "audit_metadata_trade_count": audit_count,
        "missing_audit_metadata_trade_count": missing_audit_count,
        "settlement_rows_count": settlement_rows,
        "idempotency_key_count": _count(
            session,
            select(func.count(PaperTrade.id)).where(trade_filter).where(PaperTrade.settlement_idempotency_key.is_not(None)),
        ),
        "payout_audit_count": _count(
            session,
            select(func.count(PaperTrade.id)).where(trade_filter).where(PaperTrade.settlement_payout.is_not(None)),
        ),
        "fee_adjustment_audit_count": _count(
            session,
            select(func.count(PaperTrade.id)).where(trade_filter).where(PaperTrade.settlement_fee_adjustment.is_not(None)),
        ),
    }


def _int_json(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip('"'))
    except (TypeError, ValueError):
        return None


def _bool_json(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        return value.strip('"').lower() == "true"
    return None


def _spread_audit(session: Session | None, epoch: PaperTradingEpoch | None) -> dict[str, object]:
    if session is None or epoch is None:
        return {"status": UNKNOWN_EVIDENCE}
    spread_result = JobRun.result["spread_audit"]
    row = session.execute(
        select(
            JobRun.status.label("job_status"),
            JobRun.started_at,
            JobRun.completed_at,
            JobRun.target_date,
            spread_result["checked"].label("checked"),
            spread_result["verified"].label("verified"),
            spread_result["trusted_audit_only_count"].label("trusted_audit_only_count"),
            spread_result["needs_review_count"].label("needs_review_count"),
            spread_result["unsafe_count"].label("unsafe_count"),
            spread_result["parse_error_count"].label("parse_error_count"),
            spread_result["settlement_text_unverified_count"].label("settlement_text_unverified_count"),
            spread_result["push_behavior_uncertain_count"].label("push_behavior_uncertain_count"),
            spread_result["paper_trades_created"].label("paper_trades_created"),
            spread_result["read_only"].label("read_only"),
        )
        .where(JobRun.paper_trading_epoch_id == epoch.id)
        .where(JobRun.job_name == "spread-audit")
        .order_by(JobRun.started_at.desc(), JobRun.id.desc())
        .limit(1)
    ).mappings().first()
    if row is None:
        return {"status": UNKNOWN_EVIDENCE, "latest_spread_audit_job": {"status": "not_run"}}
    result: dict[str, object] = {
        "status": row["job_status"],
        "latest_spread_audit_job": {
            "status": row["job_status"],
            "started_at": _iso(row["started_at"]),
            "completed_at": _iso(row["completed_at"]),
            "target_date": row["target_date"].isoformat() if row["target_date"] else None,
        },
    }
    for key in (
        "checked",
        "verified",
        "trusted_audit_only_count",
        "needs_review_count",
        "unsafe_count",
        "parse_error_count",
        "settlement_text_unverified_count",
        "push_behavior_uncertain_count",
        "paper_trades_created",
    ):
        value = _int_json(row[key])
        if value is not None:
            result[key] = value
    read_only = _bool_json(row["read_only"])
    if read_only is not None:
        result["read_only"] = read_only
    return result


def _component(name: str, status: str, evidence: dict[str, object]) -> dict[str, object]:
    return {"component": name, "status": status, "evidence": evidence}


def _governance_evidence_available(governance: dict[str, object]) -> bool:
    if governance.get("last_training_run") or governance.get("last_calibration_run"):
        return True
    evidence_counts = (
        governance.get("raw_resolved_mature_samples"),
        governance.get("clean_resolved_mature_samples"),
        governance.get("clean_training_eligible_count"),
    )
    return any(int(value or 0) > 0 for value in evidence_counts)


def _validated_components(
    candidate_pipeline: dict[str, object],
    settlement_audit: dict[str, object],
    spread_audit: dict[str, object],
    governance: dict[str, object],
    safety_ok: bool,
) -> list[dict[str, object]]:
    candidate_rows = int(candidate_pipeline.get("candidate_rows") or 0)
    return [
        _component("paper_only_safety_posture", _status(safety_ok), {}),
        _component(
            "pr3s_exposure_taxonomy",
            _status_from_count(int(candidate_pipeline.get("taxonomy_complete_count") or 0)),
            {
                "candidate_rows": candidate_rows,
                "complete_count": candidate_pipeline.get("taxonomy_complete_count", 0),
                "version": EXPOSURE_TAXONOMY_VERSION,
                "line_policy_version": LINE_CLASSIFICATION_POLICY_VERSION,
            },
        ),
        _component(
            "pr3t_live_like_selector",
            _status_from_count(int(candidate_pipeline.get("selector_complete_count") or 0)),
            {"policy_version": SELECTOR_POLICY_VERSION, "mode": candidate_pipeline.get("selector_mode")},
        ),
        _component(
            "pr3u_probability_adapters",
            _status_from_count(int(candidate_pipeline.get("probability_adapter_complete_count") or 0)),
            {"policy_version": PROBABILITY_ADAPTER_POLICY_VERSION},
        ),
        _component(
            "pr3v_family_scope_governance",
            "available" if _governance_evidence_available(governance) else UNKNOWN_EVIDENCE,
            {
                "policy_version": governance.get("governance_policy_version"),
                "capability_enabled": bool(governance.get("family_scope_governance_enabled")),
                "last_governance_status": governance.get("status"),
                "family_scope_unit_count": governance.get("family_scope_unit_count"),
                "raw_resolved_mature_samples": governance.get("raw_resolved_mature_samples"),
                "clean_resolved_mature_samples": governance.get("clean_resolved_mature_samples"),
                "clean_training_eligible_count": governance.get("clean_training_eligible_count"),
                "last_training_run": governance.get("last_training_run"),
                "last_calibration_run": governance.get("last_calibration_run"),
            },
        ),
        _component(
            "pr3w_probability_hardening",
            _status_from_count(int(candidate_pipeline.get("probability_hardening_core_count") or 0)),
            {"policy_version": candidate_pipeline.get("probability_hardening_policy_version")},
        ),
        _component(
            "pr3x_risk_governance",
            _status_from_count(int(candidate_pipeline.get("risk_governance_core_count") or 0)),
            {"policy_version": candidate_pipeline.get("risk_governance_policy_version")},
        ),
        _component(
            "pr4a_settlement_accounting_audit",
            _status_from_count(int(settlement_audit.get("audit_metadata_trade_count") or 0)),
            {
                "settlement_formula_version": SETTLEMENT_FORMULA_VERSION,
                "audit_metadata_trade_count": settlement_audit.get("audit_metadata_trade_count", 0),
                "missing_audit_metadata_trade_count": settlement_audit.get("missing_audit_metadata_trade_count", 0),
            },
        ),
        _component(
            "full_game_spread_trusted_audit_gate",
            "available" if int(spread_audit.get("checked") or 0) > 0 else UNKNOWN_EVIDENCE,
            {
                "checked": spread_audit.get("checked", 0),
                "trusted_audit_only_count": spread_audit.get("trusted_audit_only_count", 0),
                "read_only": spread_audit.get("read_only"),
            },
        ),
    ]


def _blockers_for_live() -> list[dict[str, object]]:
    return [
        {"code": "live_execution_intentionally_absent", "severity": "hard_blocker"},
        {"code": "live_trading_disabled_by_config", "severity": "hard_blocker"},
        {"code": "execution_kill_switch_enabled", "severity": "hard_blocker"},
        {"code": "demo_kalshi_environment", "severity": "hard_blocker"},
        {"code": "production_credentials_absent", "severity": "hard_blocker"},
        {"code": "legal_compliance_operator_approval_not_recorded", "severity": "hard_blocker"},
        {"code": "live_order_path_not_implemented", "severity": "hard_blocker"},
        {"code": "operator_bankroll_exposure_policy_not_signed_off", "severity": "hard_blocker"},
        {"code": "family_specific_live_calibration_thresholds_not_signed_off", "severity": "hard_blocker"},
        {"code": "production_rollback_kill_procedure_requires_manual_confirmation", "severity": "hard_blocker"},
    ]


def _operator_checklist() -> list[dict[str, object]]:
    return [
        {"item": "review_paper_observation_results", "status": "required"},
        {"item": "verify_settlement_audit_idempotency", "status": "required"},
        {"item": "verify_candidate_taxonomy_selector_adapter_hardening_risk_fields", "status": "required"},
        {"item": "review_family_scope_governance_units", "status": "required"},
        {"item": "review_full_game_spread_trusted_audit_rows", "status": "required"},
        {"item": "confirm_legal_compliance_and_exchange_terms", "status": "required"},
        {"item": "design_explicit_live_order_execution_pr", "status": "required_before_live"},
        {"item": "define_live_rollback_and_kill_procedure", "status": "required_before_live"},
    ]


def _safety_gates() -> tuple[dict[str, object], bool]:
    settings = get_settings()
    credentials_configured = settings.kalshi_credentials_configured
    production_credentials_configured = settings.kalshi_env == "production" and credentials_configured
    gates = {
        "paper_trading_enabled": {"status": _status(settings.paper_trading), "value": settings.paper_trading},
        "live_trading_disabled": {
            "status": _status(not settings.live_trading_enabled),
            "value": not settings.live_trading_enabled,
        },
        "execution_kill_switch_enabled": {
            "status": _status(settings.execution_kill_switch),
            "value": settings.execution_kill_switch,
        },
        "kalshi_demo_environment": {"status": _status(settings.kalshi_env == "demo"), "value": settings.kalshi_env},
        "production_credentials_absent": {
            "status": _status(not production_credentials_configured),
            "value": not production_credentials_configured,
            "credentials_configured": credentials_configured,
            "kalshi_env": settings.kalshi_env,
        },
        "websocket_market_data_disabled_by_default": {
            "status": _status(not settings.websocket_market_data_enabled),
            "value": not settings.websocket_market_data_enabled,
        },
        "live_order_path_absent": {"status": "pass", "value": True},
        "sportsbook_inputs_absent": {"status": "pass", "value": True},
        "team_totals_absent": {"status": "pass", "value": True},
        "umpire_factors_absent": {"status": "pass", "value": True},
        "mve_multivariate_absent": {"status": "pass", "value": True},
    }
    safety_ok = all(entry["status"] == "pass" for entry in gates.values())
    return gates, safety_ok


def readiness_audit_pack(session: Session | None = None) -> dict[str, object]:
    settings = get_settings()
    captured_at = utc_now()
    epoch = _active_epoch(session)
    safety_gates, safety_ok = _safety_gates()
    accounting = _accounting(session, epoch)
    governance = _model_governance(session, epoch)
    candidate_pipeline = _candidate_pipeline(session, epoch)
    settlement_audit = _settlement_audit(session, epoch)
    spread_audit = _spread_audit(session, epoch)
    blockers = _blockers_for_live()
    checklist = _operator_checklist()
    paper_status = PAPER_OBSERVATION_READY if safety_ok else PAPER_OBSERVATION_BLOCKED
    validated_components = _validated_components(
        candidate_pipeline,
        settlement_audit,
        spread_audit,
        governance,
        safety_ok,
    )
    return {
        "policy_version": READINESS_POLICY_VERSION,
        "captured_at": _iso(captured_at),
        "paper_observation_status": paper_status,
        "live_readiness_status": LIVE_READINESS_STATUS,
        "live_enabled": False,
        "operator_review_required": True,
        "bot_mode": {
            "mode": "paper",
            "paper_trading": settings.paper_trading,
            "live_trading_enabled": settings.live_trading_enabled,
            "execution_kill_switch": settings.execution_kill_switch,
            "kalshi_env": settings.kalshi_env,
            "kalshi_credentials": "set_redacted" if settings.kalshi_credentials_configured else "not_set",
            "websocket_market_data_enabled": settings.websocket_market_data_enabled,
        },
        "safety_gates": safety_gates,
        "validated_components": validated_components,
        "current_accounting": accounting,
        "model_governance": governance,
        "candidate_pipeline": candidate_pipeline,
        "settlement_audit": settlement_audit,
        "spread_audit": spread_audit,
        "blockers_for_live": blockers,
        "operator_checklist": checklist,
        "readiness_decision": {
            "paper_observation_ready": paper_status == PAPER_OBSERVATION_READY,
            "paper_observation_status": paper_status,
            "live_readiness_status": LIVE_READINESS_STATUS,
            "live_enabled": False,
            "operator_review_required": True,
            "required_next_pr": "separate_explicit_live_readiness_design_pr",
            "recommended_next_action": "Continue paper observation and operator review; do not enable live trading from this PR.",
        },
    }


def compact_readiness_summary(pack: dict[str, object]) -> dict[str, object]:
    components = pack.get("validated_components")
    component_status_counts: dict[str, int] = {}
    if isinstance(components, list):
        for item in components:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "unknown")
            component_status_counts[status] = component_status_counts.get(status, 0) + 1
    checklist = pack.get("operator_checklist")
    blocker_count = len(pack.get("blockers_for_live") or []) if isinstance(pack.get("blockers_for_live"), list) else 0
    return {
        "policy_version": pack.get("policy_version"),
        "paper_observation_status": pack.get("paper_observation_status"),
        "live_readiness_status": pack.get("live_readiness_status"),
        "live_enabled": pack.get("live_enabled"),
        "operator_review_required": pack.get("operator_review_required"),
        "readiness_decision": pack.get("readiness_decision"),
        "safety_gates": pack.get("safety_gates"),
        "validated_component_status_counts": component_status_counts,
        "live_blocker_count": blocker_count,
        "operator_checklist_count": len(checklist) if isinstance(checklist, list) else 0,
    }
