from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import CalibrationRun, KalshiMarket, ModelCandidate, ModelVersion, TrainingRun
from app.services.contracts import market_type_from_ticker
from app.services.features import FEATURE_VERSION
from app.time_utils import utc_now

HEURISTIC_MODEL_TAG = "heuristic_full_game_winner_v1"
MODEL_FAMILY = "full_game_winner"


@dataclass(frozen=True)
class ModelScore:
    probability: Decimal
    fair_value: Decimal
    rationale: dict[str, object]


def get_or_create_heuristic_model_version(session: Session) -> ModelVersion:
    version = session.scalar(select(ModelVersion).where(ModelVersion.version_tag == HEURISTIC_MODEL_TAG))
    if version is None:
        now = utc_now()
        version = ModelVersion(
            version_tag=HEURISTIC_MODEL_TAG,
            description="Transparent PR3 heuristic for full-game winner paper candidates.",
            trained_at=now,
            metrics={
                "model_type": "deterministic_heuristic",
                "uses_market_price": False,
                "promotion_policy": "active baseline until trained challenger clears governance",
            },
            is_active=False,
            model_family=MODEL_FAMILY,
            feature_version=FEATURE_VERSION,
            role="champion",
            promoted_at=now,
        )
        session.add(version)
        session.flush()
        _activate_model_version(session, version, now)
        return version

    if not version.is_active:
        _activate_model_version(session, version, utc_now())
    else:
        _deactivate_other_active_versions(session, version.id)
    return version


def _deactivate_other_active_versions(session: Session, active_id: int | None) -> None:
    active_versions = list(session.scalars(select(ModelVersion).where(ModelVersion.is_active.is_(True))))
    for active_version in active_versions:
        if active_version.id == active_id:
            continue
        active_version.is_active = False
        if active_version.role == "champion":
            active_version.role = "inactive"
        session.add(active_version)


def _activate_model_version(session: Session, version: ModelVersion, now: datetime) -> None:
    _deactivate_other_active_versions(session, version.id)
    version.is_active = True
    version.role = "champion"
    version.promoted_at = version.promoted_at or now
    session.add(version)


def score_candidate_probability(features: dict[str, object], contract_side: str = "yes") -> ModelScore:
    data_quality = Decimal(str(features.get("data_quality") or "0.10"))
    mapping_confidence = Decimal(str(features.get("mapping_confidence") or "0"))
    selected_is_home = features.get("selected_is_home") is True
    selected_is_away = features.get("selected_is_away") is True

    raw_edge = Decimal("0.0000")
    effects: list[dict[str, object]] = []

    if selected_is_home:
        raw_edge += Decimal("0.0300")
        effects.append({"feature": "home_field", "effect": 0.03})
    elif selected_is_away:
        raw_edge -= Decimal("0.0150")
        effects.append({"feature": "away_team", "effect": -0.015})

    confidence_effect = max(min(mapping_confidence - Decimal("0.75"), Decimal("0.05")), Decimal("-0.05"))
    confidence_effect = confidence_effect * Decimal("0.20")
    raw_edge += confidence_effect
    effects.append({"feature": "mapping_confidence", "effect": float(confidence_effect)})

    capped_edge = max(min(raw_edge, Decimal("0.0800")), Decimal("-0.0800"))
    shrunk_edge = capped_edge * data_quality
    probability = Decimal("0.500000") + shrunk_edge
    if contract_side.lower() == "no":
        probability = Decimal("1.000000") - probability
    probability = max(min(probability, Decimal("0.650000")), Decimal("0.350000"))
    probability = probability.quantize(Decimal("0.000001"))

    return ModelScore(
        probability=probability,
        fair_value=probability.quantize(Decimal("0.0001")),
        rationale={
            "model_version": HEURISTIC_MODEL_TAG,
            "feature_version": FEATURE_VERSION,
            "model_family": MODEL_FAMILY,
            "base_probability": 0.5,
            "raw_edge": float(raw_edge),
            "capped_edge": float(capped_edge),
            "data_quality": float(data_quality),
            "uses_market_price": False,
            "effects": effects,
        },
    )


def _resolved_supported_candidates(session: Session) -> list[ModelCandidate]:
    rows = session.execute(
        select(ModelCandidate, KalshiMarket)
        .outerjoin(KalshiMarket, ModelCandidate.kalshi_market_id == KalshiMarket.id)
        .where(ModelCandidate.outcome.in_(["win", "loss"]))
        .where(ModelCandidate.training_eligible.is_(True))
    )
    return [
        candidate
        for candidate, market in rows
        if market_type_from_ticker(market.ticker if market else None, candidate.market_type) == MODEL_FAMILY
    ]


def _repair_resolved_candidate_market_families(session: Session) -> int:
    repaired = 0
    for candidate in _resolved_supported_candidates(session):
        if candidate.market_type != MODEL_FAMILY:
            candidate.market_type = MODEL_FAMILY
            session.add(candidate)
            repaired += 1
    if repaired:
        session.flush()
    return repaired


def _resolved_sample_count(session: Session) -> int:
    return len(_resolved_supported_candidates(session))


def run_model_governance(session: Session, now: datetime | None = None) -> dict[str, object]:
    settings = get_settings()
    started = now or utc_now()
    active = get_or_create_heuristic_model_version(session)
    repaired_candidates = _repair_resolved_candidate_market_families(session)
    sample_count = _resolved_sample_count(session)
    minimum = settings.model_training_min_samples

    training = TrainingRun(
        model_version_id=active.id,
        started_at=started,
        completed_at=started,
        candidate_count=sample_count,
        metrics={
            "model_family": MODEL_FAMILY,
            "minimum_samples": minimum,
            "sample_count": sample_count,
            "repaired_candidate_market_types": repaired_candidates,
            "validation_policy": "chronological_holdout_required",
            "promotion_policy": "promote only after challenger beats active on out-of-sample log loss and brier",
        },
    )
    calibration = CalibrationRun(
        model_version_id=active.id,
        started_at=started,
        completed_at=started,
        method="platt_sigmoid_pending",
        metrics={
            "model_family": MODEL_FAMILY,
            "minimum_samples": minimum,
            "sample_count": sample_count,
            "repaired_candidate_market_types": repaired_candidates,
            "calibration_policy": "skip tiny samples to avoid overfit",
        },
    )

    if sample_count < minimum:
        reason = f"INSUFFICIENT_RESOLVED_SAMPLES:{sample_count}/{minimum}"
        training.status = "skipped"
        calibration.status = "skipped"
        training.metrics = {**(training.metrics or {}), "reason": reason}
        calibration.metrics = {**(calibration.metrics or {}), "reason": reason}
        promoted = False
    else:
        reason = "TRAINED_MODEL_PROMOTION_NOT_ENABLED_IN_PR3"
        training.status = "skipped"
        calibration.status = "skipped"
        training.metrics = {**(training.metrics or {}), "reason": reason}
        calibration.metrics = {**(calibration.metrics or {}), "reason": reason}
        promoted = False

    session.add(training)
    session.add(calibration)
    session.commit()
    return {
        "status": training.status,
        "reason": reason,
        "resolved_samples": sample_count,
        "repaired_candidate_market_types": repaired_candidates,
        "minimum_samples": minimum,
        "active_model_version": active.version_tag,
        "training_run_id": training.id,
        "calibration_run_id": calibration.id,
        "promoted": promoted,
    }
