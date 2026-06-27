from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from math import exp, factorial, log
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import CalibrationRun, MlbGame, ModelCandidate, ModelGovernanceEvent, ModelVersion, TrainingRun
from app.services.contracts import (
    FIRST_FIVE_SPREAD,
    FIRST_FIVE_TOTAL,
    FIRST_FIVE_WINNER,
    FULL_GAME_SPREAD,
    FULL_GAME_TOTAL,
    FULL_GAME_WINNER,
    PAPER_SUPPORTED_MARKET_FAMILIES,
)
from app.services.features import FEATURE_VERSION, LEAGUE_AVG_FULL_GAME_RUNS
from app.time_utils import ensure_aware_utc, get_dashboard_zone, utc_now

HEURISTIC_MODEL_TAG = "heuristic_full_game_winner_v1"
MATURE_MODEL_TAG = "mature_mlb_run_distribution_v1"
MODEL_FAMILY = "mlb_all_supported_families"
MAX_RUNS = 18


@dataclass(frozen=True)
class ModelScore:
    probability: Decimal
    fair_value: Decimal
    rationale: dict[str, object]
    probability_raw: Decimal | None = None
    probability_calibrated: Decimal | None = None
    data_quality: Decimal | None = None
    calibration_status: str | None = None
    training_eligible: bool = False
    training_exclusion_reason: str | None = None
    push_probability: Decimal | None = None


@dataclass(frozen=True)
class RunExpectations:
    away_full_game_runs_mean: Decimal
    home_full_game_runs_mean: Decimal
    away_first_five_runs_mean: Decimal
    home_first_five_runs_mean: Decimal
    effects: list[dict[str, object]]


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


def get_or_create_mature_model_version(session: Session) -> ModelVersion:
    version = session.scalar(select(ModelVersion).where(ModelVersion.version_tag == MATURE_MODEL_TAG))
    if version is None:
        now = utc_now()
        version = ModelVersion(
            version_tag=MATURE_MODEL_TAG,
            description="PR3c transparent MLB run-distribution model for all supported Kalshi market families.",
            trained_at=now,
            metrics={
                "model_type": "run_distribution_v1",
                "uses_market_price": False,
                "supported_families": sorted(PAPER_SUPPORTED_MARKET_FAMILIES),
                "distribution": "independent poisson enumeration with conservative shrinkage",
                "promotion_policy": "champion until trained challenger clears governance",
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


def get_or_create_heuristic_model_version(session: Session) -> ModelVersion:
    return get_or_create_mature_model_version(session)


def _decimal(value: object, default: Decimal = Decimal("0")) -> Decimal:
    try:
        if value is None:
            return default
        return Decimal(str(value))
    except Exception:
        return default


def _bounded(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return max(min(value, high), low)


def _module(features: dict[str, object], name: str) -> dict[str, Any]:
    value = features.get(name)
    return value if isinstance(value, dict) else {}


def _nested_decimal(value: dict[str, Any], *path: str, default: Decimal = Decimal("0")) -> Decimal:
    current: Any = value
    for key in path:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    return _decimal(current, default)


def expected_runs(features: dict[str, object]) -> RunExpectations:
    context = _module(features, "game_context")
    team_prior = _module(features, "team_strength_prior")
    travel = _module(features, "travel_schedule")
    starter = _module(features, "starter")
    park_weather = _module(features, "park_weather")

    home_full = LEAGUE_AVG_FULL_GAME_RUNS
    away_full = LEAGUE_AVG_FULL_GAME_RUNS
    effects: list[dict[str, object]] = []

    home_field = Decimal("0.18")
    home_full += home_field
    away_full -= Decimal("0.04")
    effects.append({"feature": "home_field", "home_runs": float(home_field), "away_runs": -0.04})

    home_pct = _nested_decimal(team_prior, "home", "win_pct", default=Decimal("0.5000"))
    away_pct = _nested_decimal(team_prior, "away", "win_pct", default=Decimal("0.5000"))
    prior_delta = _bounded((home_pct - away_pct) * Decimal("1.10"), Decimal("-0.45"), Decimal("0.45"))
    home_full += prior_delta
    away_full -= prior_delta
    effects.append({"feature": "team_strength_prior", "home_runs": float(prior_delta), "away_runs": float(-prior_delta)})

    if context.get("day_night") == "day":
        home_full += Decimal("0.04")
        away_full += Decimal("0.04")
        effects.append({"feature": "day_game_run_environment", "both_teams": 0.04})

    venue = park_weather.get("park") if isinstance(park_weather.get("park"), dict) else {}
    venue_name = str(venue.get("name") or "").lower()
    if any(token in venue_name for token in ("coors", "fenway", "great american")):
        home_full += Decimal("0.18")
        away_full += Decimal("0.18")
        effects.append({"feature": "park_factor_proxy", "both_teams": 0.18, "source": venue_name})

    home_rest = _decimal(travel.get("home_rest_days"), Decimal("1"))
    away_rest = _decimal(travel.get("away_rest_days"), Decimal("1"))
    rest_delta = _bounded((home_rest - away_rest) * Decimal("0.025"), Decimal("-0.08"), Decimal("0.08"))
    home_full += rest_delta
    away_full -= rest_delta
    effects.append({"feature": "rest_days", "home_runs": float(rest_delta), "away_runs": float(-rest_delta)})

    home_starter = starter.get("home") if isinstance(starter.get("home"), dict) else {}
    away_starter = starter.get("away") if isinstance(starter.get("away"), dict) else {}
    if home_starter.get("source_status") != "available":
        away_full += Decimal("0.05")
        effects.append({"feature": "missing_home_starter", "away_runs": 0.05})
    if away_starter.get("source_status") != "available":
        home_full += Decimal("0.05")
        effects.append({"feature": "missing_away_starter", "home_runs": 0.05})

    home_full = _bounded(home_full, Decimal("2.40"), Decimal("7.20")).quantize(Decimal("0.0001"))
    away_full = _bounded(away_full, Decimal("2.40"), Decimal("7.20")).quantize(Decimal("0.0001"))
    home_first_five = _bounded(home_full * Decimal("0.49"), Decimal("1.00"), Decimal("4.20")).quantize(Decimal("0.0001"))
    away_first_five = _bounded(away_full * Decimal("0.49"), Decimal("1.00"), Decimal("4.20")).quantize(Decimal("0.0001"))

    return RunExpectations(
        away_full_game_runs_mean=away_full,
        home_full_game_runs_mean=home_full,
        away_first_five_runs_mean=away_first_five,
        home_first_five_runs_mean=home_first_five,
        effects=effects,
    )


def _poisson_distribution(mean: Decimal, max_runs: int = MAX_RUNS) -> list[Decimal]:
    lam = float(mean)
    probabilities = [Decimal(str(exp(-lam) * (lam**runs) / factorial(runs))) for runs in range(max_runs)]
    tail = Decimal("1") - sum(probabilities)
    probabilities.append(max(tail, Decimal("0")))
    return [prob.quantize(Decimal("0.00000001")) for prob in probabilities]


def _joint_probability(
    away_dist: list[Decimal],
    home_dist: list[Decimal],
    predicate,
) -> Decimal:
    total = Decimal("0")
    for away_runs, away_prob in enumerate(away_dist):
        for home_runs, home_prob in enumerate(home_dist):
            if predicate(away_runs, home_runs):
                total += away_prob * home_prob
    return _bounded(total, Decimal("0"), Decimal("1")).quantize(Decimal("0.000001"))


def _conditional_no_tie_probability(win_probability: Decimal, tie_probability: Decimal) -> Decimal:
    non_tie_probability = Decimal("1.000000") - tie_probability
    if non_tie_probability <= Decimal("0"):
        return Decimal("0.500000")
    return _bounded(win_probability / non_tie_probability, Decimal("0"), Decimal("1")).quantize(Decimal("0.000001"))


def _selected_side(features: dict[str, object]) -> str | None:
    market_context = _module(features, "market_context")
    selected = market_context.get("selection_code")
    return str(selected).upper() if selected else None


def _line_value(features: dict[str, object]) -> Decimal | None:
    value = _module(features, "market_context").get("line_value")
    if value is None:
        return None
    return _decimal(value).quantize(Decimal("0.0001"))


def _over_under(features: dict[str, object]) -> str | None:
    value = _module(features, "market_context").get("over_under_side")
    return str(value).lower() if value else None


def _probability_from_distribution(
    features: dict[str, object],
    market_type: str,
    expectations: RunExpectations,
) -> tuple[Decimal, Decimal]:
    context = _module(features, "game_context")
    home_code = str(context.get("home_abbreviation") or "").upper()
    away_code = str(context.get("away_abbreviation") or "").upper()
    selected = _selected_side(features)
    line = _line_value(features)
    side = _over_under(features)

    if market_type.startswith("first_five"):
        away_dist = _poisson_distribution(expectations.away_first_five_runs_mean)
        home_dist = _poisson_distribution(expectations.home_first_five_runs_mean)
    else:
        away_dist = _poisson_distribution(expectations.away_full_game_runs_mean)
        home_dist = _poisson_distribution(expectations.home_full_game_runs_mean)

    push_probability = Decimal("0.000000")
    if market_type in {FULL_GAME_WINNER, FIRST_FIVE_WINNER}:
        if selected == "TIE":
            probability = _joint_probability(away_dist, home_dist, lambda away, home: away == home)
        elif selected == home_code:
            probability = _joint_probability(away_dist, home_dist, lambda away, home: home > away)
            if market_type == FULL_GAME_WINNER:
                tie_probability = _joint_probability(away_dist, home_dist, lambda away, home: away == home)
                probability = _conditional_no_tie_probability(probability, tie_probability)
        elif selected == away_code:
            probability = _joint_probability(away_dist, home_dist, lambda away, home: away > home)
            if market_type == FULL_GAME_WINNER:
                tie_probability = _joint_probability(away_dist, home_dist, lambda away, home: away == home)
                probability = _conditional_no_tie_probability(probability, tie_probability)
        else:
            probability = Decimal("0.000000")
    elif market_type in {FULL_GAME_SPREAD, FIRST_FIVE_SPREAD} and line is not None:
        if selected == home_code:
            probability = _joint_probability(away_dist, home_dist, lambda away, home: Decimal(home - away) + line > 0)
            push_probability = _joint_probability(away_dist, home_dist, lambda away, home: Decimal(home - away) + line == 0)
        elif selected == away_code:
            probability = _joint_probability(away_dist, home_dist, lambda away, home: Decimal(away - home) + line > 0)
            push_probability = _joint_probability(away_dist, home_dist, lambda away, home: Decimal(away - home) + line == 0)
        else:
            probability = Decimal("0.000000")
    elif market_type in {FULL_GAME_TOTAL, FIRST_FIVE_TOTAL} and line is not None:
        if side == "over":
            probability = _joint_probability(away_dist, home_dist, lambda away, home: Decimal(away + home) > line)
        elif side == "under":
            probability = _joint_probability(away_dist, home_dist, lambda away, home: Decimal(away + home) < line)
        else:
            probability = Decimal("0.000000")
        push_probability = _joint_probability(away_dist, home_dist, lambda away, home: Decimal(away + home) == line)
    else:
        probability = Decimal("0.000000")

    return probability, push_probability


def _calibrate_probability(raw_probability: Decimal, data_quality: Decimal) -> tuple[Decimal, str]:
    shrink = Decimal("0.35") if data_quality < Decimal("0.70") else Decimal("0.20")
    calibrated = Decimal("0.500000") + (raw_probability - Decimal("0.500000")) * (Decimal("1.0") - shrink)
    return _bounded(calibrated, Decimal("0.020000"), Decimal("0.980000")).quantize(Decimal("0.000001")), "insufficient_samples"


def _training_eligibility(features: dict[str, object], market_type: str, settlement_status: str | None) -> tuple[bool, str | None]:
    data_quality = _decimal(features.get("data_quality"), Decimal("0"))
    context = _module(features, "data_quality_summary").get("context")
    if features.get("feature_version") != FEATURE_VERSION:
        return False, "non_mature_feature_version"
    if market_type not in PAPER_SUPPORTED_MARKET_FAMILIES:
        return False, "unsupported_market_family"
    if settlement_status != "paper_supported" and market_type != FULL_GAME_WINNER:
        return False, "not_paper_supported_mapping"
    if context == "post_start":
        return False, "candidate_after_game_start"
    if data_quality < Decimal("0.45"):
        return False, "insufficient_feature_quality"
    return True, None


def score_mature_candidate(
    features: dict[str, object],
    *,
    market_type: str,
    settlement_status: str | None,
) -> ModelScore:
    expectations = expected_runs(features)
    raw_probability, push_probability = _probability_from_distribution(features, market_type, expectations)
    data_quality = _decimal(features.get("data_quality"), Decimal("0.10")).quantize(Decimal("0.0001"))
    calibrated, calibration_status = _calibrate_probability(raw_probability, data_quality)
    training_eligible, training_exclusion_reason = _training_eligibility(features, market_type, settlement_status)

    return ModelScore(
        probability=calibrated,
        probability_raw=raw_probability,
        probability_calibrated=calibrated,
        fair_value=calibrated.quantize(Decimal("0.0001")),
        data_quality=data_quality,
        calibration_status=calibration_status,
        training_eligible=training_eligible,
        training_exclusion_reason=training_exclusion_reason,
        push_probability=push_probability,
        rationale={
            "model_version": MATURE_MODEL_TAG,
            "feature_version": FEATURE_VERSION,
            "model_family": market_type,
            "uses_market_price": False,
            "distribution": "independent_poisson_enumeration_v1",
            "run_expectations": {
                "away_full_game_runs_mean": float(expectations.away_full_game_runs_mean),
                "home_full_game_runs_mean": float(expectations.home_full_game_runs_mean),
                "away_first_five_runs_mean": float(expectations.away_first_five_runs_mean),
                "home_first_five_runs_mean": float(expectations.home_first_five_runs_mean),
            },
            "probability_raw": float(raw_probability),
            "probability_calibrated": float(calibrated),
            "push_probability": float(push_probability),
            "data_quality": float(data_quality),
            "calibration_status": calibration_status,
            "effects": expectations.effects,
        },
    )


def score_candidate_probability(features: dict[str, object], contract_side: str = "yes") -> ModelScore:
    market_type = str(_module(features, "market_context").get("market_family") or FULL_GAME_WINNER)
    score = score_mature_candidate(features, market_type=market_type, settlement_status="paper_supported")
    if contract_side.lower() == "no":
        probability = Decimal("1.000000") - score.probability
        return ModelScore(
            probability=probability.quantize(Decimal("0.000001")),
            fair_value=probability.quantize(Decimal("0.0001")),
            rationale=score.rationale,
            probability_raw=(Decimal("1.000000") - (score.probability_raw or score.probability)).quantize(Decimal("0.000001")),
            probability_calibrated=probability.quantize(Decimal("0.000001")),
            data_quality=score.data_quality,
            calibration_status=score.calibration_status,
            training_eligible=score.training_eligible,
            training_exclusion_reason=score.training_exclusion_reason,
            push_probability=score.push_probability,
        )
    return score


def _candidate_outcome_value(candidate: ModelCandidate) -> int | None:
    if candidate.outcome == "win":
        return 1
    if candidate.outcome == "loss":
        return 0
    return None


def _candidate_target_date_matches_game(candidate: ModelCandidate, game: MlbGame | None) -> bool:
    if candidate.target_date is None or game is None:
        return False
    game_day = ensure_aware_utc(game.scheduled_start).astimezone(get_dashboard_zone()).date()
    return candidate.target_date == game_day


def _resolved_mature_candidates(session: Session) -> list[ModelCandidate]:
    rows = list(
        session.execute(
            select(ModelCandidate, MlbGame)
            .outerjoin(MlbGame, ModelCandidate.mlb_game_id == MlbGame.id)
            .where(ModelCandidate.outcome.in_(["win", "loss"]))
            .where(ModelCandidate.training_eligible.is_(True))
            .where(ModelCandidate.feature_version == FEATURE_VERSION)
            .where(ModelCandidate.market_family.in_(PAPER_SUPPORTED_MARKET_FAMILIES))
            .where(ModelCandidate.fee_estimate.is_not(None))
            .where(ModelCandidate.price_status == "fresh_executable")
            .where(ModelCandidate.time_to_start_minutes.is_not(None))
            .where(ModelCandidate.time_to_start_minutes > 0)
            .order_by(ModelCandidate.resolved_at.asc().nullslast(), ModelCandidate.evaluated_at.asc())
        )
    )
    return [candidate for candidate, game in rows if _candidate_target_date_matches_game(candidate, game)]


def _metrics(candidates: list[ModelCandidate]) -> dict[str, object]:
    rows: list[tuple[float, int, str | None, str | None]] = []
    for candidate in candidates:
        outcome = _candidate_outcome_value(candidate)
        probability = candidate.probability_calibrated or candidate.model_probability or candidate.probability
        if outcome is None or probability is None:
            continue
        prob = min(max(float(probability), 0.000001), 0.999999)
        rows.append((prob, outcome, candidate.market_family, candidate.time_bucket))
    if not rows:
        return {"sample_count": 0, "brier_score": None, "log_loss": None, "expected_calibration_error": None}
    brier = sum((prob - outcome) ** 2 for prob, outcome, _family, _bucket in rows) / len(rows)
    log_loss = -sum(outcome * log(prob) + (1 - outcome) * log(1 - prob) for prob, outcome, _family, _bucket in rows) / len(rows)
    bins: list[dict[str, object]] = []
    ece = 0.0
    for index in range(10):
        low = index / 10
        high = (index + 1) / 10
        bucket_rows = [(prob, outcome) for prob, outcome, _family, _bucket in rows if low <= prob < high or (index == 9 and prob == 1)]
        if not bucket_rows:
            continue
        avg_prob = sum(prob for prob, _outcome in bucket_rows) / len(bucket_rows)
        observed = sum(outcome for _prob, outcome in bucket_rows) / len(bucket_rows)
        weight = len(bucket_rows) / len(rows)
        ece += abs(avg_prob - observed) * weight
        bins.append({"low": low, "high": high, "count": len(bucket_rows), "avg_probability": avg_prob, "observed_rate": observed})
    family_counts: dict[str, int] = {}
    bucket_counts: dict[str, int] = {}
    for _prob, _outcome, family, bucket in rows:
        family_counts[str(family or "unknown")] = family_counts.get(str(family or "unknown"), 0) + 1
        bucket_counts[str(bucket or "unknown")] = bucket_counts.get(str(bucket or "unknown"), 0) + 1
    return {
        "sample_count": len(rows),
        "brier_score": brier,
        "log_loss": log_loss,
        "expected_calibration_error": ece,
        "reliability_bins": bins,
        "market_family_breakdown": family_counts,
        "time_bucket_breakdown": bucket_counts,
    }


def run_model_governance(session: Session, now: datetime | None = None) -> dict[str, object]:
    settings = get_settings()
    started = now or utc_now()
    active = get_or_create_mature_model_version(session)
    candidates = _resolved_mature_candidates(session)
    sample_count = len(candidates)
    train_min = settings.model_min_samples_train
    calibrate_min = settings.model_min_samples_calibrate
    promote_min = settings.model_min_samples_promote
    metrics = _metrics(candidates)

    training = TrainingRun(
        model_version_id=active.id,
        started_at=started,
        completed_at=started,
        candidate_count=sample_count,
        metrics={
            **metrics,
            "model_version": active.version_tag,
            "feature_version": FEATURE_VERSION,
            "minimum_samples_train": train_min,
            "minimum_samples_promote": promote_min,
            "split_policy": "chronological_holdout",
            "excluded_feature_versions": ["market_family_wire_v1_pre_full_model", "mlb_features_v1"],
        },
    )
    calibration = CalibrationRun(
        model_version_id=active.id,
        started_at=started,
        completed_at=started,
        method="platt_sigmoid_when_threshold_met",
        metrics={
            **metrics,
            "minimum_samples_calibrate": calibrate_min,
            "calibration_policy": "skip until mature resolved sample threshold",
        },
    )

    if sample_count < train_min:
        status = "skipped_insufficient_samples"
        reason = f"INSUFFICIENT_MATURE_RESOLVED_SAMPLES:{sample_count}/{train_min}"
        promoted = False
    else:
        status = "trained_not_promoted"
        reason = "CHALLENGER_TRAINING_PLACEHOLDER_TRANSPARENT_MODEL_RETAINED"
        promoted = False

    training.status = status
    calibration.status = "skipped_insufficient_samples" if sample_count < calibrate_min else "trained_not_promoted"
    training.metrics = {**(training.metrics or {}), "reason": reason}
    calibration.metrics = {**(calibration.metrics or {}), "reason": reason}
    event = ModelGovernanceEvent(
        occurred_at=started,
        event_type="model_governance",
        status=status,
        details={
            "reason": reason,
            "sample_count": sample_count,
            "active_model_version": active.version_tag,
            "promoted": promoted,
            "metrics": metrics,
        },
    )
    session.add_all([training, calibration, event])
    session.commit()
    return {
        "status": status,
        "reason": reason,
        "resolved_samples": sample_count,
        "resolved_mature_samples": sample_count,
        "minimum_samples_train": train_min,
        "minimum_samples_calibrate": calibrate_min,
        "minimum_samples_promote": promote_min,
        "active_model_version": active.version_tag,
        "feature_version": FEATURE_VERSION,
        "training_run_id": training.id,
        "calibration_run_id": calibration.id,
        "promoted": promoted,
        "metrics": metrics,
    }


def governance_status(session: Session) -> dict[str, object]:
    active = session.scalar(select(ModelVersion).where(ModelVersion.is_active.is_(True)))
    last_training = session.scalar(select(TrainingRun).order_by(TrainingRun.started_at.desc()))
    last_calibration = session.scalar(select(CalibrationRun).order_by(CalibrationRun.started_at.desc()))
    mature_count = session.scalar(
        select(func.count(ModelCandidate.id))
        .where(ModelCandidate.feature_version == FEATURE_VERSION)
        .where(ModelCandidate.training_eligible.is_(True))
    ) or 0
    resolved_count = len(_resolved_mature_candidates(session))
    return {
        "active_model_version": active.version_tag if active else None,
        "feature_version": FEATURE_VERSION,
        "calibration_status": last_calibration.status if last_calibration else "not_run",
        "last_training_run": last_training.started_at.isoformat() if last_training else None,
        "last_calibration_run": last_calibration.started_at.isoformat() if last_calibration else None,
        "resolved_mature_samples": resolved_count,
        "training_eligible_count": int(mature_count),
        "last_governance_status": last_training.status if last_training else "not_run",
        "notes": "Mature PR3c model is active; calibration is skipped until sample thresholds are met.",
    }


def repair_training_eligibility(session: Session) -> dict[str, object]:
    repaired = 0
    candidates = list(
        session.scalars(
            select(ModelCandidate).where(
                (ModelCandidate.feature_version.is_(None))
                | (ModelCandidate.feature_version != FEATURE_VERSION)
                | (ModelCandidate.model_version_tag != MATURE_MODEL_TAG)
            )
        )
    )
    for candidate in candidates:
        if candidate.training_eligible:
            candidate.training_eligible = False
            candidate.training_exclusion_reason = "pre_pr3c_or_non_mature_model"
            session.add(candidate)
            repaired += 1
    session.commit()
    return {
        "candidates_checked": len(candidates),
        "candidates_marked_ineligible": repaired,
        "feature_version_required": FEATURE_VERSION,
        "model_version_required": MATURE_MODEL_TAG,
    }
