from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from math import exp, factorial, log
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    CalibrationRun,
    MlbGame,
    ModelCandidate,
    ModelGovernanceEvent,
    ModelParameterVersion,
    ModelThresholdVersion,
    ModelTrainingDataset,
    ModelVersion,
    TrainingRun,
)
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
from app.services.paper_epoch import get_or_create_active_paper_epoch
from app.time_utils import ensure_aware_utc, get_dashboard_zone, utc_now

HEURISTIC_MODEL_TAG = "heuristic_full_game_winner_v1"
MATURE_MODEL_TAG = "mature_mlb_run_distribution_v2"
BASELINE_PARAMETER_VERSION_TAG = "mature_mlb_run_distribution_v2_baseline"
MODEL_FAMILY = "mlb_all_supported_families"
MAX_RUNS = 18
GOVERNANCE_CLEAN_TRAINING_POLICY = "pr3p_clean_governance_training_v1"
GOVERNANCE_CLEAN_TRAINING_ZONE = ZoneInfo("America/New_York")
DEFAULT_MODEL_PARAMETERS: dict[str, object] = {
    "league_average_full_game_runs": 4.35,
    "home_field_runs": 0.18,
    "away_field_runs": -0.04,
    "team_strength_coefficient": 1.10,
    "team_strength_cap": 0.45,
    "offense_runs_coefficient": 0.35,
    "starter_era_coefficient": 0.12,
    "bullpen_era_coefficient": 0.08,
    "lineup_confirmed_runs": 0.04,
    "handedness_known_runs": 0.02,
    "day_game_runs": 0.04,
    "park_factor_coefficient": 0.75,
    "weather_temperature_coefficient": 0.003,
    "weather_wind_coefficient": 0.004,
    "rest_days_coefficient": 0.025,
    "rest_days_cap": 0.08,
    "missing_starter_penalty": 0.05,
    "full_game_run_min": 2.40,
    "full_game_run_max": 7.20,
    "first_five_base_share": 0.49,
    "first_five_starter_known_bonus": 0.015,
    "first_five_lineup_known_bonus": 0.010,
    "first_five_run_min": 1.00,
    "first_five_run_max": 4.20,
    "calibration_shrink_low_quality": 0.35,
    "calibration_shrink_high_quality": 0.20,
    "market_family_probability_offsets": {},
    "feature_module_weights_version": "family_weighted_v2",
    "spread_push_policy": "no_trade_when_push_possible",
    "total_push_policy": "no_trade_when_push_possible",
}

GOVERNANCE_PARAMETER_REGISTRY: dict[str, list[dict[str, object]]] = {
    "governed_now": [
        {
            "name": "market_family_probability_offsets",
            "storage": "model_parameter_versions.parameters",
            "status": "autonomous_bounded_challenger",
            "guardrails": [
                "MODEL_MIN_SAMPLES_TRAIN",
                "MODEL_MIN_SAMPLES_PROMOTE",
                "MODEL_PROMOTION_MIN_LOGLOSS_IMPROVEMENT",
                "MODEL_PROMOTION_MAX_ECE",
            ],
        },
        {
            "name": "active_parameter_version",
            "storage": "model_parameter_versions.is_active",
            "status": "autonomous_promotion_guarded",
            "guardrails": ["chronological_holdout", "active_paper_epoch", "clean_training_cutoff"],
        },
    ],
    "future_governable": [
        {
            "name": "run_expectation_coefficients",
            "status": "static_until_enough_clean_samples",
            "examples": [
                "team_strength_coefficient",
                "starter_era_coefficient",
                "bullpen_era_coefficient",
                "park_factor_coefficient",
            ],
        },
        {
            "name": "trade_threshold_policy",
            "status": "recorded_for_simulation_only",
            "examples": ["paper_min_net_ev", "paper_min_prob_edge", "paper_observation_min_data_quality"],
        },
        {
            "name": "feature_module_weights",
            "status": "diagnostic_static_until_validation",
            "examples": ["family_weighted_v2"],
        },
    ],
    "intentionally_static_safety": [
        {
            "name": "execution_safety_flags",
            "status": "manual_only",
            "examples": ["LIVE_TRADING_ENABLED", "EXECUTION_KILL_SWITCH", "KALSHI_ENV"],
        },
        {
            "name": "paper_risk_caps",
            "status": "manual_operator_policy",
            "examples": [
                "PAPER_MAX_DAILY_NEW_RISK_PCT",
                "PAPER_MAX_OPEN_RISK_PCT",
                "PAPER_MAX_SCOPE_RISK_PCT",
            ],
        },
        {
            "name": "spread_activation",
            "status": "manual_only",
            "examples": ["PAPER_SPREAD_TRADING_ENABLED"],
        },
    ],
}


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


def _deactivate_other_parameter_versions(session: Session, active_id: int | None) -> None:
    active_versions = list(
        session.scalars(select(ModelParameterVersion).where(ModelParameterVersion.is_active.is_(True)))
    )
    for active_version in active_versions:
        if active_version.id == active_id:
            continue
        active_version.is_active = False
        if active_version.role == "champion":
            active_version.role = "inactive"
        session.add(active_version)


def _activate_parameter_version(session: Session, version: ModelParameterVersion, now: datetime) -> None:
    _deactivate_other_parameter_versions(session, version.id)
    version.is_active = True
    version.role = "champion"
    version.status = "active"
    version.promoted_at = version.promoted_at or now
    session.add(version)


def get_or_create_active_parameter_version(session: Session) -> ModelParameterVersion:
    active_version = session.scalar(
        select(ModelParameterVersion)
        .where(ModelParameterVersion.is_active.is_(True))
        .order_by(ModelParameterVersion.promoted_at.desc(), ModelParameterVersion.id.desc())
    )
    if active_version is not None:
        _deactivate_other_parameter_versions(session, active_version.id)
        return active_version

    version = session.scalar(
        select(ModelParameterVersion).where(ModelParameterVersion.version_tag == BASELINE_PARAMETER_VERSION_TAG)
    )
    if version is None:
        now = utc_now()
        version = ModelParameterVersion(
            version_tag=BASELINE_PARAMETER_VERSION_TAG,
            model_family=MODEL_FAMILY,
            role="champion",
            status="active",
            is_active=False,
            created_reason="PR3c fix2 baseline parameterization of formerly static model knobs.",
            trained_at=now,
            promoted_at=now,
            parameters=DEFAULT_MODEL_PARAMETERS,
            metrics={
                "parameter_type": "transparent_bounded_run_distribution",
                "uses_market_price": False,
                "training_policy": "baseline until challenger passes out-of-sample guardrails",
            },
        )
        session.add(version)
        session.flush()
        _activate_parameter_version(session, version, now)
        return version

    _activate_parameter_version(session, version, utc_now())
    return version


def active_parameter_payload(session: Session) -> dict[str, object]:
    version = get_or_create_active_parameter_version(session)
    return {
        "version_tag": version.version_tag,
        "model_family": version.model_family,
        "role": version.role,
        "status": version.status,
        "is_active": version.is_active,
        "trained_at": version.trained_at.isoformat() if version.trained_at else None,
        "promoted_at": version.promoted_at.isoformat() if version.promoted_at else None,
        "parameters": version.parameters,
        "metrics": version.metrics,
    }


def get_or_create_mature_model_version(session: Session) -> ModelVersion:
    version = session.scalar(select(ModelVersion).where(ModelVersion.version_tag == MATURE_MODEL_TAG))
    if version is None:
        now = utc_now()
        version = ModelVersion(
            version_tag=MATURE_MODEL_TAG,
            description="PR3c fix2 trainable MLB run-distribution model for all supported Kalshi market families.",
            trained_at=now,
            metrics={
                "model_type": "run_distribution_v2",
                "uses_market_price": False,
                "supported_families": sorted(PAPER_SUPPORTED_MARKET_FAMILIES),
                "distribution": "parameterized independent poisson enumeration with conservative shrinkage",
                "parameter_version": BASELINE_PARAMETER_VERSION_TAG,
                "promotion_policy": "parameter challenger must clear chronological holdout guardrails",
            },
            is_active=False,
            model_family=MODEL_FAMILY,
            feature_version=FEATURE_VERSION,
            role="champion",
            promoted_at=now,
        )
        session.add(version)
        session.flush()
        get_or_create_active_parameter_version(session)
        _activate_model_version(session, version, now)
        return version

    get_or_create_active_parameter_version(session)
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


def _param_decimal(parameters: dict[str, object], key: str, default: Decimal) -> Decimal:
    return _decimal(parameters.get(key), default)


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


def _nested_optional_decimal(value: dict[str, Any], *path: str) -> Decimal | None:
    current: Any = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    if current is None:
        return None
    return _decimal(current, Decimal("0"))


def _status_available(value: dict[str, Any], *path: str) -> bool:
    current: Any = value
    for key in path:
        if not isinstance(current, dict):
            return False
        current = current.get(key)
    return isinstance(current, dict) and current.get("source_status") == "available"


def _offense_effect(
    offense: dict[str, Any],
    recent: dict[str, Any],
    side: str,
    coefficient: Decimal,
) -> Decimal:
    season_runs = _nested_optional_decimal(offense, side, "runs_per_game")
    recent_runs = _nested_optional_decimal(recent, side, "runs_per_game")
    values = [value for value in (season_runs, recent_runs) if value is not None]
    if not values:
        return Decimal("0")
    blended = sum(values) / Decimal(len(values))
    return _bounded((blended - LEAGUE_AVG_FULL_GAME_RUNS) * coefficient, Decimal("-0.35"), Decimal("0.35"))


def expected_runs(
    features: dict[str, object],
    parameters: dict[str, object] | None = None,
) -> RunExpectations:
    parameters = parameters or DEFAULT_MODEL_PARAMETERS
    context = _module(features, "game_context")
    team_prior = _module(features, "team_strength_prior")
    offense = _module(features, "offense_season")
    recent_offense = _module(features, "offense_recent")
    travel = _module(features, "travel_schedule")
    starter_identity = _module(features, "starter_identity")
    starter_season = _module(features, "starter_season")
    bullpen = _module(features, "bullpen_season")
    lineup = _module(features, "lineup")
    handedness = _module(features, "handedness_platoon")
    park_weather = _module(features, "park_weather")

    home_full = _param_decimal(parameters, "league_average_full_game_runs", LEAGUE_AVG_FULL_GAME_RUNS)
    away_full = _param_decimal(parameters, "league_average_full_game_runs", LEAGUE_AVG_FULL_GAME_RUNS)
    effects: list[dict[str, object]] = []

    home_field = _param_decimal(parameters, "home_field_runs", Decimal("0.18"))
    away_field = _param_decimal(parameters, "away_field_runs", Decimal("-0.04"))
    home_full += home_field
    away_full += away_field
    effects.append({"feature": "home_field", "home_runs": float(home_field), "away_runs": float(away_field)})

    home_pct = _nested_decimal(team_prior, "home", "win_pct", default=Decimal("0.5000"))
    away_pct = _nested_decimal(team_prior, "away", "win_pct", default=Decimal("0.5000"))
    strength_coefficient = _param_decimal(parameters, "team_strength_coefficient", Decimal("1.10"))
    strength_cap = _param_decimal(parameters, "team_strength_cap", Decimal("0.45"))
    prior_delta = _bounded((home_pct - away_pct) * strength_coefficient, -strength_cap, strength_cap)
    home_full += prior_delta
    away_full -= prior_delta
    effects.append({"feature": "team_strength_prior", "home_runs": float(prior_delta), "away_runs": float(-prior_delta)})

    offense_coefficient = _param_decimal(parameters, "offense_runs_coefficient", Decimal("0.35"))
    home_offense = _offense_effect(offense, recent_offense, "home", offense_coefficient)
    away_offense = _offense_effect(offense, recent_offense, "away", offense_coefficient)
    home_full += home_offense
    away_full += away_offense
    effects.append({"feature": "offense", "home_runs": float(home_offense), "away_runs": float(away_offense)})

    starter_coefficient = _param_decimal(parameters, "starter_era_coefficient", Decimal("0.12"))
    away_starter_era = _nested_optional_decimal(starter_season, "away", "season", "era")
    home_starter_era = _nested_optional_decimal(starter_season, "home", "season", "era")
    if away_starter_era is not None:
        home_starter_effect = (away_starter_era - LEAGUE_AVG_FULL_GAME_RUNS) * starter_coefficient
        home_starter_effect = _bounded(home_starter_effect, Decimal("-0.30"), Decimal("0.30"))
        home_full += home_starter_effect
        effects.append({"feature": "opposing_starter", "home_runs": float(home_starter_effect)})
    if home_starter_era is not None:
        away_starter_effect = (home_starter_era - LEAGUE_AVG_FULL_GAME_RUNS) * starter_coefficient
        away_starter_effect = _bounded(away_starter_effect, Decimal("-0.30"), Decimal("0.30"))
        away_full += away_starter_effect
        effects.append({"feature": "opposing_starter", "away_runs": float(away_starter_effect)})

    bullpen_coefficient = _param_decimal(parameters, "bullpen_era_coefficient", Decimal("0.08"))
    away_bullpen_era = _nested_optional_decimal(bullpen, "away", "era")
    home_bullpen_era = _nested_optional_decimal(bullpen, "home", "era")
    if away_bullpen_era is not None:
        effect = _bounded((away_bullpen_era - LEAGUE_AVG_FULL_GAME_RUNS) * bullpen_coefficient, Decimal("-0.20"), Decimal("0.20"))
        home_full += effect
        effects.append({"feature": "bullpen", "home_runs": float(effect)})
    if home_bullpen_era is not None:
        effect = _bounded((home_bullpen_era - LEAGUE_AVG_FULL_GAME_RUNS) * bullpen_coefficient, Decimal("-0.20"), Decimal("0.20"))
        away_full += effect
        effects.append({"feature": "bullpen", "away_runs": float(effect)})

    lineup_effect = _param_decimal(parameters, "lineup_confirmed_runs", Decimal("0.04"))
    if _status_available(lineup, "home"):
        home_full += lineup_effect
        effects.append({"feature": "lineup_confirmed", "home_runs": float(lineup_effect)})
    if _status_available(lineup, "away"):
        away_full += lineup_effect
        effects.append({"feature": "lineup_confirmed", "away_runs": float(lineup_effect)})

    handedness_effect = _param_decimal(parameters, "handedness_known_runs", Decimal("0.02"))
    if handedness.get("source_status") in {"available", "partial"}:
        home_full += handedness_effect
        away_full += handedness_effect
        effects.append({"feature": "handedness_known", "both_teams": float(handedness_effect)})

    if context.get("day_night") == "day":
        day_effect = _param_decimal(parameters, "day_game_runs", Decimal("0.04"))
        home_full += day_effect
        away_full += day_effect
        effects.append({"feature": "day_game_run_environment", "both_teams": float(day_effect)})

    venue = park_weather.get("park") if isinstance(park_weather.get("park"), dict) else {}
    run_factor = _decimal(venue.get("run_factor"), Decimal("1.0"))
    park_coefficient = _param_decimal(parameters, "park_factor_coefficient", Decimal("0.75"))
    park_effect = _bounded((run_factor - Decimal("1.0")) * park_coefficient, Decimal("-0.25"), Decimal("0.35"))
    if park_effect:
        home_full += park_effect
        away_full += park_effect
        effects.append({"feature": "park_factor", "both_teams": float(park_effect)})

    weather = park_weather.get("weather") if isinstance(park_weather.get("weather"), dict) else {}
    temperature = _decimal(weather.get("temperature_2m"), Decimal("70"))
    wind_speed = _decimal(weather.get("wind_speed_10m"), Decimal("0"))
    temp_coeff = _param_decimal(parameters, "weather_temperature_coefficient", Decimal("0.003"))
    wind_coeff = _param_decimal(parameters, "weather_wind_coefficient", Decimal("0.004"))
    weather_effect = _bounded(((temperature - Decimal("70")) * temp_coeff) + (wind_speed * wind_coeff), Decimal("-0.18"), Decimal("0.25"))
    if weather and weather_effect:
        home_full += weather_effect
        away_full += weather_effect
        effects.append({"feature": "weather", "both_teams": float(weather_effect)})

    home_rest = _nested_decimal(travel, "home", "rest_days", default=Decimal("1"))
    away_rest = _nested_decimal(travel, "away", "rest_days", default=Decimal("1"))
    rest_coefficient = _param_decimal(parameters, "rest_days_coefficient", Decimal("0.025"))
    rest_cap = _param_decimal(parameters, "rest_days_cap", Decimal("0.08"))
    rest_delta = _bounded((home_rest - away_rest) * rest_coefficient, -rest_cap, rest_cap)
    home_full += rest_delta
    away_full -= rest_delta
    effects.append({"feature": "rest_days", "home_runs": float(rest_delta), "away_runs": float(-rest_delta)})

    missing_starter_penalty = _param_decimal(parameters, "missing_starter_penalty", Decimal("0.05"))
    home_starter = starter_identity.get("home") if isinstance(starter_identity.get("home"), dict) else {}
    away_starter = starter_identity.get("away") if isinstance(starter_identity.get("away"), dict) else {}
    if home_starter.get("source_status") != "available":
        away_full += missing_starter_penalty
        effects.append({"feature": "missing_home_starter", "away_runs": float(missing_starter_penalty)})
    if away_starter.get("source_status") != "available":
        home_full += missing_starter_penalty
        effects.append({"feature": "missing_away_starter", "home_runs": float(missing_starter_penalty)})

    full_min = _param_decimal(parameters, "full_game_run_min", Decimal("2.40"))
    full_max = _param_decimal(parameters, "full_game_run_max", Decimal("7.20"))
    home_full = _bounded(home_full, full_min, full_max).quantize(Decimal("0.0001"))
    away_full = _bounded(away_full, full_min, full_max).quantize(Decimal("0.0001"))

    first_five_share = _param_decimal(parameters, "first_five_base_share", Decimal("0.49"))
    starter_bonus = _param_decimal(parameters, "first_five_starter_known_bonus", Decimal("0.015"))
    lineup_bonus = _param_decimal(parameters, "first_five_lineup_known_bonus", Decimal("0.010"))
    home_f5_share = first_five_share
    away_f5_share = first_five_share
    if home_starter.get("source_status") == "available":
        away_f5_share -= starter_bonus
    if away_starter.get("source_status") == "available":
        home_f5_share -= starter_bonus
    if _status_available(lineup, "home"):
        home_f5_share += lineup_bonus
    if _status_available(lineup, "away"):
        away_f5_share += lineup_bonus
    f5_min = _param_decimal(parameters, "first_five_run_min", Decimal("1.00"))
    f5_max = _param_decimal(parameters, "first_five_run_max", Decimal("4.20"))
    home_first_five = _bounded(home_full * home_f5_share, f5_min, f5_max).quantize(Decimal("0.0001"))
    away_first_five = _bounded(away_full * away_f5_share, f5_min, f5_max).quantize(Decimal("0.0001"))

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


def _calibrate_probability(
    raw_probability: Decimal,
    data_quality: Decimal,
    *,
    market_type: str,
    parameters: dict[str, object] | None = None,
) -> tuple[Decimal, str]:
    parameters = parameters or DEFAULT_MODEL_PARAMETERS
    low_quality_shrink = _param_decimal(parameters, "calibration_shrink_low_quality", Decimal("0.35"))
    high_quality_shrink = _param_decimal(parameters, "calibration_shrink_high_quality", Decimal("0.20"))
    shrink = low_quality_shrink if data_quality < Decimal("0.70") else high_quality_shrink
    calibrated = Decimal("0.500000") + (raw_probability - Decimal("0.500000")) * (Decimal("1.0") - shrink)
    offsets = parameters.get("market_family_probability_offsets")
    if isinstance(offsets, dict):
        calibrated += _decimal(offsets.get("__global__"), Decimal("0"))
        calibrated += _decimal(offsets.get(market_type), Decimal("0"))
    status = "trained_parameterized" if parameters.get("trained_from_samples") else "baseline_parameterized"
    return _bounded(calibrated, Decimal("0.020000"), Decimal("0.980000")).quantize(Decimal("0.000001")), status


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
    parameters: dict[str, object] | None = None,
    parameter_version_tag: str | None = None,
) -> ModelScore:
    parameters = parameters or DEFAULT_MODEL_PARAMETERS
    parameter_version_tag = parameter_version_tag or BASELINE_PARAMETER_VERSION_TAG
    expectations = expected_runs(features, parameters)
    raw_probability, push_probability = _probability_from_distribution(features, market_type, expectations)
    data_quality = _decimal(features.get("data_quality"), Decimal("0.10")).quantize(Decimal("0.0001"))
    calibrated, calibration_status = _calibrate_probability(
        raw_probability,
        data_quality,
        market_type=market_type,
        parameters=parameters,
    )
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
            "parameter_version": parameter_version_tag,
            "feature_version": FEATURE_VERSION,
            "model_family": market_type,
            "uses_market_price": False,
            "distribution": "parameterized_independent_poisson_enumeration_v2",
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
            "parameter_snapshot": parameters,
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


def _governance_clean_training_window(settings=None) -> dict[str, object]:
    settings = settings or get_settings()
    raw_value = str(settings.model_governance_clean_start_at or "").strip()
    if not raw_value:
        raw_value = "2026-07-02T00:00:00-04:00"
    try:
        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            "MODEL_GOVERNANCE_CLEAN_START_AT must be an ISO date/time, for example 2026-07-02T00:00:00-04:00"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=GOVERNANCE_CLEAN_TRAINING_ZONE)
    start_utc = ensure_aware_utc(parsed)
    start_et = start_utc.astimezone(GOVERNANCE_CLEAN_TRAINING_ZONE)
    return {
        "policy": GOVERNANCE_CLEAN_TRAINING_POLICY,
        "start_at": start_utc,
        "start_at_utc": start_utc.isoformat(),
        "start_at_et": start_et.isoformat(),
        "start_date_et": start_et.date(),
        "start_date_et_iso": start_et.date().isoformat(),
    }


def governance_parameter_registry() -> dict[str, object]:
    return {
        "policy": "autonomous_parameter_coverage_v1",
        "governed_now_count": len(GOVERNANCE_PARAMETER_REGISTRY["governed_now"]),
        "future_governable_count": len(GOVERNANCE_PARAMETER_REGISTRY["future_governable"]),
        "intentionally_static_safety_count": len(GOVERNANCE_PARAMETER_REGISTRY["intentionally_static_safety"]),
        **GOVERNANCE_PARAMETER_REGISTRY,
    }


def governance_parameter_registry_summary() -> dict[str, object]:
    registry = governance_parameter_registry()
    return {
        "policy": registry["policy"],
        "governed_now_count": registry["governed_now_count"],
        "future_governable_count": registry["future_governable_count"],
        "intentionally_static_safety_count": registry["intentionally_static_safety_count"],
    }


def _resolved_mature_candidates(session: Session, paper_trading_epoch_id: int | None = None) -> list[ModelCandidate]:
    stmt = (
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
    )
    if paper_trading_epoch_id is not None:
        stmt = stmt.where(ModelCandidate.paper_trading_epoch_id == paper_trading_epoch_id)
    rows = list(
        session.execute(
            stmt.order_by(ModelCandidate.resolved_at.asc().nullslast(), ModelCandidate.evaluated_at.asc())
        )
    )
    return [candidate for candidate, game in rows if _candidate_target_date_matches_game(candidate, game)]


def _clean_candidate_exclusion_reason(candidate: ModelCandidate, clean_window: dict[str, object]) -> str | None:
    clean_start = clean_window["start_at"]
    clean_date = clean_window["start_date_et"]
    if candidate.target_date is None:
        return "missing_target_date"
    if candidate.target_date < clean_date:
        return "target_date_before_clean_start"
    if candidate.evaluated_at is None:
        return "missing_evaluated_at"
    if ensure_aware_utc(candidate.evaluated_at) < clean_start:
        return "evaluated_before_clean_start"
    return None


def _apply_clean_training_filter(
    candidates: list[ModelCandidate],
    clean_window: dict[str, object],
) -> tuple[list[ModelCandidate], dict[str, int]]:
    included: list[ModelCandidate] = []
    excluded_counts: dict[str, int] = {}
    for candidate in candidates:
        reason = _clean_candidate_exclusion_reason(candidate, clean_window)
        if reason is None:
            included.append(candidate)
        else:
            excluded_counts[reason] = excluded_counts.get(reason, 0) + 1
    return included, excluded_counts


def _governance_training_policy_payload(
    clean_window: dict[str, object],
    *,
    raw_sample_count: int,
    clean_sample_count: int,
    excluded_counts: dict[str, int],
) -> dict[str, object]:
    return {
        "governance_training_policy": clean_window["policy"],
        "clean_training_start_at": clean_window["start_at_utc"],
        "clean_training_start_at_et": clean_window["start_at_et"],
        "clean_training_start_date_et": clean_window["start_date_et_iso"],
        "raw_resolved_mature_samples": raw_sample_count,
        "clean_resolved_mature_samples": clean_sample_count,
        "pre_clean_excluded_samples": raw_sample_count - clean_sample_count,
        "clean_filter_exclusion_counts": excluded_counts,
    }


def governance_sample_summary(session: Session, paper_trading_epoch_id: int | None = None) -> dict[str, object]:
    if paper_trading_epoch_id is None:
        paper_trading_epoch_id = get_or_create_active_paper_epoch(session).id
    clean_window = _governance_clean_training_window()
    raw_candidates = _resolved_mature_candidates(session, paper_trading_epoch_id=paper_trading_epoch_id)
    clean_candidates, excluded_counts = _apply_clean_training_filter(raw_candidates, clean_window)
    return {
        "paper_trading_epoch_id": paper_trading_epoch_id,
        **_governance_training_policy_payload(
            clean_window,
            raw_sample_count=len(raw_candidates),
            clean_sample_count=len(clean_candidates),
            excluded_counts=excluded_counts,
        ),
    }


def _candidate_probability(candidate: ModelCandidate, offsets: dict[str, Decimal] | None = None) -> Decimal | None:
    probability = candidate.probability_calibrated or candidate.model_probability or candidate.probability
    if probability is None:
        return None
    adjusted = probability
    if offsets:
        adjusted += offsets.get(candidate.market_family or "unknown", Decimal("0"))
        adjusted += offsets.get("__global__", Decimal("0"))
    return _bounded(adjusted, Decimal("0.000001"), Decimal("0.999999")).quantize(Decimal("0.000001"))


def _metrics(
    candidates: list[ModelCandidate],
    offsets: dict[str, Decimal] | None = None,
) -> dict[str, object]:
    rows: list[tuple[float, int, str | None, str | None]] = []
    for candidate in candidates:
        outcome = _candidate_outcome_value(candidate)
        probability = _candidate_probability(candidate, offsets)
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


def _chronological_split(candidates: list[ModelCandidate]) -> tuple[list[ModelCandidate], list[ModelCandidate]]:
    if len(candidates) < 2:
        return candidates, []
    split_index = max(1, int(len(candidates) * 0.7))
    return candidates[:split_index], candidates[split_index:]


def _fit_probability_offsets(candidates: list[ModelCandidate]) -> dict[str, Decimal]:
    rows: list[tuple[Decimal, Decimal, str]] = []
    for candidate in candidates:
        outcome = _candidate_outcome_value(candidate)
        probability = _candidate_probability(candidate)
        if outcome is None or probability is None:
            continue
        rows.append((probability, Decimal(outcome), candidate.market_family or "unknown"))
    if not rows:
        return {"__global__": Decimal("0")}
    avg_error = sum(outcome - probability for probability, outcome, _family in rows) / Decimal(len(rows))
    global_offset = _bounded(avg_error, Decimal("-0.075"), Decimal("0.075")).quantize(Decimal("0.000001"))
    offsets = {"__global__": global_offset}
    family_groups: dict[str, list[tuple[Decimal, Decimal]]] = {}
    for probability, outcome, family in rows:
        family_groups.setdefault(family, []).append((probability, outcome))
    min_family_samples = get_settings().model_min_family_samples_for_family_calibration
    for family, family_rows in family_groups.items():
        if len(family_rows) < min_family_samples:
            continue
        family_error = sum(outcome - probability for probability, outcome in family_rows) / Decimal(len(family_rows))
        residual_error = family_error - global_offset
        offsets[family] = _bounded(residual_error, Decimal("-0.050"), Decimal("0.050")).quantize(Decimal("0.000001"))
    return offsets


def _offsets_to_json(offsets: dict[str, Decimal]) -> dict[str, float]:
    return {key: float(value) for key, value in offsets.items()}


def _parameter_offsets(parameters: dict[str, object] | None) -> dict[str, Decimal]:
    offsets = (parameters or {}).get("market_family_probability_offsets") if parameters else None
    if not isinstance(offsets, dict):
        return {}
    return {
        str(key): _decimal(value, Decimal("0")).quantize(Decimal("0.000001"))
        for key, value in offsets.items()
    }


def _combine_probability_offsets(
    parameters: dict[str, object] | None,
    residual_offsets: dict[str, Decimal],
) -> dict[str, Decimal]:
    existing_offsets = _parameter_offsets(parameters)
    keys = set(existing_offsets) | set(residual_offsets)
    return {
        key: (existing_offsets.get(key, Decimal("0")) + residual_offsets.get(key, Decimal("0"))).quantize(
            Decimal("0.000001")
        )
        for key in keys
    }


def _clean_challenger_parameter_seed(
    active_parameters: ModelParameterVersion,
    clean_window: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    if _metrics_match_clean_training_policy(active_parameters.metrics, clean_window):
        return dict(active_parameters.parameters or DEFAULT_MODEL_PARAMETERS), {
            "parameter_seed_version": active_parameters.version_tag,
            "parameter_seed_policy": "inherit_clean_active_parameter_version",
            "parameter_seed_clean_policy_matched": True,
        }
    return dict(DEFAULT_MODEL_PARAMETERS), {
        "parameter_seed_version": active_parameters.version_tag,
        "parameter_seed_policy": "reset_to_default_parameters_pre_clean_active_ignored",
        "parameter_seed_clean_policy_matched": False,
    }


def _metadata_epoch_id(metadata: dict[str, object] | None) -> int | None:
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("paper_trading_epoch_id")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _metrics_match_clean_training_policy(
    metadata: dict[str, object] | None,
    clean_window: dict[str, object] | None = None,
) -> bool:
    if not isinstance(metadata, dict):
        return False
    clean_window = clean_window or _governance_clean_training_window()
    return (
        metadata.get("governance_training_policy") == clean_window["policy"]
        and metadata.get("clean_training_start_at") == clean_window["start_at_utc"]
    )


def _latest_row_by_metrics_epoch(
    session: Session,
    model,
    order_column,
    paper_trading_epoch_id: int,
    clean_window: dict[str, object] | None = None,
):
    for row in session.scalars(select(model).order_by(order_column.desc(), model.id.desc())):
        metrics = getattr(row, "metrics", None)
        if _metadata_epoch_id(metrics) == paper_trading_epoch_id and _metrics_match_clean_training_policy(
            metrics, clean_window
        ):
            return row
    return None


def _threshold_matches_governance_policy(
    session: Session,
    threshold: ModelThresholdVersion,
    paper_trading_epoch_id: int,
    clean_window: dict[str, object] | None = None,
) -> bool:
    if _metadata_epoch_id(threshold.metrics) == paper_trading_epoch_id and _metrics_match_clean_training_policy(
        threshold.metrics, clean_window
    ):
        return True
    if threshold.source_training_run_id is None:
        return False
    training = session.get(TrainingRun, threshold.source_training_run_id)
    return (
        training is not None
        and _metadata_epoch_id(training.metrics) == paper_trading_epoch_id
        and _metrics_match_clean_training_policy(training.metrics, clean_window)
    )


def _governance_ignored_artifacts(
    session: Session,
    paper_trading_epoch_id: int,
    clean_window: dict[str, object] | None = None,
) -> dict[str, object]:
    clean_window = clean_window or _governance_clean_training_window()

    def summarize_rows(model, order_column) -> dict[str, object]:
        rows = [
            row
            for row in session.scalars(select(model).order_by(order_column.desc(), model.id.desc()))
            if _metadata_epoch_id(getattr(row, "metrics", None)) == paper_trading_epoch_id
            and not _metrics_match_clean_training_policy(getattr(row, "metrics", None), clean_window)
        ]
        latest = rows[0] if rows else None
        return {
            "ignored_count": len(rows),
            "latest_ignored_status": getattr(latest, "status", None) if latest else None,
            "latest_ignored_at": getattr(latest, "started_at", None).isoformat() if latest else None,
            "reason": "pre_clean_or_legacy_governance_policy",
        }

    threshold_rows = [
        threshold
        for threshold in session.scalars(
            select(ModelThresholdVersion).order_by(
                ModelThresholdVersion.created_at.desc(), ModelThresholdVersion.id.desc()
            )
        )
        if (
            _metadata_epoch_id(threshold.metrics) == paper_trading_epoch_id
            or (
                threshold.source_training_run_id is not None
                and (training := session.get(TrainingRun, threshold.source_training_run_id)) is not None
                and _metadata_epoch_id(training.metrics) == paper_trading_epoch_id
            )
        )
        and not _threshold_matches_governance_policy(session, threshold, paper_trading_epoch_id, clean_window)
    ]
    latest_threshold = threshold_rows[0] if threshold_rows else None
    return {
        "training": summarize_rows(TrainingRun, TrainingRun.started_at),
        "calibration": summarize_rows(CalibrationRun, CalibrationRun.started_at),
        "thresholds": {
            "ignored_count": len(threshold_rows),
            "latest_ignored_status": latest_threshold.status if latest_threshold else None,
            "latest_ignored_at": latest_threshold.created_at_snapshot.isoformat() if latest_threshold else None,
            "reason": "pre_clean_or_legacy_governance_policy",
        },
    }


def latest_governance_artifacts(
    session: Session,
    paper_trading_epoch_id: int,
) -> tuple[TrainingRun | None, CalibrationRun | None, ModelThresholdVersion | None]:
    clean_window = _governance_clean_training_window()
    last_training = _latest_row_by_metrics_epoch(
        session, TrainingRun, TrainingRun.started_at, paper_trading_epoch_id, clean_window
    )
    last_calibration = _latest_row_by_metrics_epoch(
        session, CalibrationRun, CalibrationRun.started_at, paper_trading_epoch_id, clean_window
    )
    last_threshold = next(
        (
            threshold
            for threshold in session.scalars(
                select(ModelThresholdVersion).order_by(ModelThresholdVersion.created_at.desc(), ModelThresholdVersion.id.desc())
            )
            if _threshold_matches_governance_policy(session, threshold, paper_trading_epoch_id, clean_window)
        ),
        None,
    )
    return last_training, last_calibration, last_threshold


def run_model_governance(
    session: Session,
    now: datetime | None = None,
    *,
    paper_trading_epoch_id: int | None = None,
) -> dict[str, object]:
    settings = get_settings()
    started = now or utc_now()
    active = get_or_create_mature_model_version(session)
    active_parameters = get_or_create_active_parameter_version(session)
    if paper_trading_epoch_id is None:
        paper_trading_epoch_id = get_or_create_active_paper_epoch(session).id
    clean_window = _governance_clean_training_window(settings)
    raw_candidates = _resolved_mature_candidates(session, paper_trading_epoch_id=paper_trading_epoch_id)
    candidates, clean_exclusion_counts = _apply_clean_training_filter(raw_candidates, clean_window)
    raw_sample_count = len(raw_candidates)
    sample_count = len(candidates)
    train_min = settings.model_min_samples_train
    calibrate_min = settings.model_min_samples_calibrate
    promote_min = settings.model_min_samples_promote
    train_rows, holdout_rows = _chronological_split(candidates)
    metrics = _metrics(candidates)
    holdout_metrics = _metrics(holdout_rows) if holdout_rows else metrics
    clean_policy_metrics = _governance_training_policy_payload(
        clean_window,
        raw_sample_count=raw_sample_count,
        clean_sample_count=sample_count,
        excluded_counts=clean_exclusion_counts,
    )

    training = TrainingRun(
        model_version_id=active.id,
        started_at=started,
        completed_at=started,
        candidate_count=sample_count,
        metrics={
            **metrics,
            "model_version": active.version_tag,
            "feature_version": FEATURE_VERSION,
            "active_parameter_version": active_parameters.version_tag,
            "paper_trading_epoch_id": paper_trading_epoch_id,
            **clean_policy_metrics,
            "minimum_samples_train": train_min,
            "minimum_samples_promote": promote_min,
            "split_policy": "chronological_holdout",
            "excluded_feature_versions": [
                "market_family_wire_v1_pre_full_model",
                "mlb_features_v1",
                "mature_mlb_features_v1",
            ],
        },
    )
    session.add(training)
    session.flush()
    dataset = ModelTrainingDataset(
        training_run_id=training.id,
        created_at_snapshot=started,
        feature_version=FEATURE_VERSION,
        sample_count=sample_count,
        split_policy="chronological_70_30_holdout",
        filters={
            "training_eligible": True,
            "feature_version": FEATURE_VERSION,
            "price_status": "fresh_executable",
            "post_start": "excluded",
            "void_push": "excluded",
            "unsupported_mapping": "excluded",
            "paper_trading_epoch_id": paper_trading_epoch_id,
            **clean_policy_metrics,
        },
        candidate_ids=[candidate.id for candidate in candidates if candidate.id is not None],
    )
    session.add(dataset)
    calibration = CalibrationRun(
        model_version_id=active.id,
        started_at=started,
        completed_at=started,
        method="platt_sigmoid_when_threshold_met",
        metrics={
            **metrics,
            "minimum_samples_calibrate": calibrate_min,
            "minimum_samples_for_isotonic": settings.model_min_samples_for_isotonic,
            "paper_trading_epoch_id": paper_trading_epoch_id,
            **clean_policy_metrics,
            "calibration_policy": "bounded family/global offsets; isotonic requires higher sample threshold",
        },
    )

    challenger: ModelParameterVersion | None = None
    threshold_version: ModelThresholdVersion | None = None
    if sample_count < train_min:
        status = "skipped_insufficient_samples"
        reason = (
            f"INSUFFICIENT_MATURE_RESOLVED_SAMPLES_CLEAN_WINDOW:{sample_count}/{train_min};"
            f"RAW_RESOLVED_MATURE_SAMPLES:{raw_sample_count}"
        )
        promoted = False
    else:
        offsets = _fit_probability_offsets(train_rows)
        parameter_seed, parameter_seed_metrics = _clean_challenger_parameter_seed(active_parameters, clean_window)
        combined_offsets = _combine_probability_offsets(parameter_seed, offsets)
        challenger_parameters = {
            **parameter_seed,
            "market_family_probability_offsets": _offsets_to_json(combined_offsets),
            "trained_from_samples": True,
            "training_sample_count": len(train_rows),
            "holdout_sample_count": len(holdout_rows),
        }
        challenger_metrics = _metrics(holdout_rows or candidates, offsets)
        challenger = ModelParameterVersion(
            version_tag=f"mature_mlb_run_distribution_v2_challenger_{training.id}",
            model_family=MODEL_FAMILY,
            role="challenger",
            status="trained",
            is_active=False,
            created_reason="Governance-trained bounded calibration offsets from resolved mature candidates.",
            trained_at=started,
            source_training_run_id=training.id,
            parameters=challenger_parameters,
            metrics={
                "train_sample_count": len(train_rows),
                "holdout_sample_count": len(holdout_rows),
                "baseline_holdout": holdout_metrics,
                "challenger_holdout": challenger_metrics,
                "residual_offsets": _offsets_to_json(offsets),
                "combined_offsets": _offsets_to_json(combined_offsets),
                "paper_trading_epoch_id": paper_trading_epoch_id,
                **parameter_seed_metrics,
                **clean_policy_metrics,
            },
        )
        session.add(challenger)
        threshold_version = ModelThresholdVersion(
            version_tag=f"trade_threshold_eval_{training.id}",
            role="evaluation",
            status="recorded",
            is_active=False,
            created_at_snapshot=started,
            source_training_run_id=training.id,
            thresholds={
                "paper_min_net_ev_current": float(settings.paper_min_net_ev),
                "paper_min_prob_edge_current": float(settings.paper_min_prob_edge),
                "policy": "simulation_only_no_auto_loosen_until_thresholds_met",
            },
            metrics={
                "sample_count": sample_count,
                "paper_trading_epoch_id": paper_trading_epoch_id,
                **clean_policy_metrics,
                "note": "Threshold tuning is evaluated separately from probability training.",
            },
        )
        session.add(threshold_version)
        baseline_logloss = holdout_metrics.get("log_loss")
        challenger_logloss = challenger_metrics.get("log_loss")
        challenger_ece = challenger_metrics.get("expected_calibration_error")
        improvement = None
        if isinstance(baseline_logloss, float) and isinstance(challenger_logloss, float):
            improvement = baseline_logloss - challenger_logloss
        can_promote = (
            sample_count >= promote_min
            and improvement is not None
            and improvement >= float(settings.model_promotion_min_logloss_improvement)
            and isinstance(challenger_ece, float)
            and challenger_ece <= float(settings.model_promotion_max_ece)
        )
        if can_promote:
            _activate_parameter_version(session, challenger, started)
            status = "promoted"
            reason = "CHALLENGER_PARAMETER_VERSION_PROMOTED"
            promoted = True
        else:
            status = "trained_not_promoted"
            reason = "CHALLENGER_DID_NOT_CLEAR_PROMOTION_GUARDRAILS"
            promoted = False

    training.status = status
    calibration.status = "skipped_insufficient_samples" if sample_count < calibrate_min else status
    training.metrics = {
        **(training.metrics or {}),
        "reason": reason,
        "holdout_metrics": holdout_metrics,
        "challenger_parameter_version": challenger.version_tag if challenger else None,
    }
    calibration.metrics = {
        **(calibration.metrics or {}),
        "reason": reason,
        "method_selected": "platt_sigmoid" if sample_count >= calibrate_min else "none",
        "isotonic_allowed": sample_count >= settings.model_min_samples_for_isotonic,
        "paper_trading_epoch_id": paper_trading_epoch_id,
    }
    event = ModelGovernanceEvent(
        occurred_at=started,
        event_type="model_governance",
        status=status,
        details={
            "reason": reason,
            "sample_count": sample_count,
            "active_model_version": active.version_tag,
            "active_parameter_version": (
                challenger.version_tag if promoted and challenger else active_parameters.version_tag
            ),
            "challenger_parameter_version": challenger.version_tag if challenger else None,
            "paper_trading_epoch_id": paper_trading_epoch_id,
            "promoted": promoted,
            "metrics": metrics,
            "holdout_metrics": holdout_metrics,
            **clean_policy_metrics,
        },
    )
    session.add_all([training, calibration, event])
    session.commit()
    return {
        "status": status,
        "reason": reason,
        "resolved_samples": sample_count,
        "resolved_mature_samples": sample_count,
        "raw_resolved_mature_samples": raw_sample_count,
        "clean_resolved_mature_samples": sample_count,
        "pre_clean_excluded_samples": raw_sample_count - sample_count,
        "clean_filter_exclusion_counts": clean_exclusion_counts,
        "governance_training_policy": clean_window["policy"],
        "clean_training_start_at": clean_window["start_at_utc"],
        "clean_training_start_at_et": clean_window["start_at_et"],
        "clean_training_start_date_et": clean_window["start_date_et_iso"],
        "minimum_samples_train": train_min,
        "minimum_samples_calibrate": calibrate_min,
        "minimum_samples_promote": promote_min,
        "active_model_version": active.version_tag,
        "active_parameter_version": (
            challenger.version_tag if promoted and challenger else active_parameters.version_tag
        ),
        "challenger_parameter_version": challenger.version_tag if challenger else None,
        "paper_trading_epoch_id": paper_trading_epoch_id,
        "feature_version": FEATURE_VERSION,
        "training_run_id": training.id,
        "calibration_run_id": calibration.id,
        "training_dataset_id": dataset.id,
        "threshold_version_id": threshold_version.id if threshold_version else None,
        "promoted": promoted,
        "metrics": metrics,
        "holdout_metrics": holdout_metrics,
        "governance_parameter_registry": governance_parameter_registry(),
    }


def governance_status(
    session: Session,
    paper_trading_epoch_id: int | None = None,
    *,
    include_details: bool = False,
) -> dict[str, object]:
    if paper_trading_epoch_id is None:
        paper_trading_epoch_id = get_or_create_active_paper_epoch(session).id
    clean_window = _governance_clean_training_window()
    active = session.scalar(select(ModelVersion).where(ModelVersion.is_active.is_(True)))
    active_parameters = session.scalar(
        select(ModelParameterVersion).where(ModelParameterVersion.is_active.is_(True))
    )
    last_training, last_calibration, last_threshold = latest_governance_artifacts(session, paper_trading_epoch_id)
    sample_summary = governance_sample_summary(session, paper_trading_epoch_id)
    ignored_artifacts = _governance_ignored_artifacts(session, paper_trading_epoch_id, clean_window)
    mature_count = session.scalar(
        select(func.count(ModelCandidate.id))
        .where(ModelCandidate.paper_trading_epoch_id == paper_trading_epoch_id)
        .where(ModelCandidate.feature_version == FEATURE_VERSION)
        .where(ModelCandidate.training_eligible.is_(True))
    ) or 0
    return {
        "active_model_version": active.version_tag if active else None,
        "active_parameter_version": active_parameters.version_tag if active_parameters else None,
        "active_calibration_version": active_parameters.version_tag if active_parameters else None,
        "paper_trading_epoch_id": paper_trading_epoch_id,
        "feature_version": FEATURE_VERSION,
        "governance_training_policy": clean_window["policy"],
        "clean_training_start_at": clean_window["start_at_utc"],
        "clean_training_start_at_et": clean_window["start_at_et"],
        "clean_training_start_date_et": clean_window["start_date_et_iso"],
        "calibration_status": last_calibration.status if last_calibration else "not_run",
        "last_training_run": last_training.started_at.isoformat() if last_training else None,
        "last_calibration_run": last_calibration.started_at.isoformat() if last_calibration else None,
        "resolved_mature_samples": int(sample_summary["clean_resolved_mature_samples"]),
        "raw_resolved_mature_samples": int(sample_summary["raw_resolved_mature_samples"]),
        "clean_resolved_mature_samples": int(sample_summary["clean_resolved_mature_samples"]),
        "pre_clean_excluded_samples": int(sample_summary["pre_clean_excluded_samples"]),
        "clean_filter_exclusion_counts": sample_summary["clean_filter_exclusion_counts"],
        "training_eligible_count": int(mature_count),
        "clean_training_eligible_count": int(sample_summary["clean_resolved_mature_samples"]),
        "last_governance_status": last_training.status if last_training else "not_run",
        "trade_threshold_policy": last_threshold.thresholds if last_threshold else {},
        "ignored_pre_clean_artifacts": ignored_artifacts,
        "governance_parameter_registry": (
            governance_parameter_registry() if include_details else governance_parameter_registry_summary()
        ),
        "notes": "PR3p clean governance trains and promotes only from active-epoch samples after the clean cutoff.",
    }


def latest_training_summary(session: Session) -> dict[str, object]:
    training = session.scalar(select(TrainingRun).order_by(TrainingRun.started_at.desc()))
    calibration = session.scalar(select(CalibrationRun).order_by(CalibrationRun.started_at.desc()))
    parameter = session.scalar(select(ModelParameterVersion).order_by(ModelParameterVersion.updated_at.desc()))
    if training is None:
        return {"status": "not_run", "training_run": None}
    return {
        "status": training.status,
        "training_run_id": training.id,
        "started_at": training.started_at.isoformat(),
        "completed_at": training.completed_at.isoformat() if training.completed_at else None,
        "candidate_count": training.candidate_count,
        "metrics": training.metrics,
        "latest_calibration": {
            "id": calibration.id if calibration else None,
            "status": calibration.status if calibration else None,
            "method": calibration.method if calibration else None,
            "metrics": calibration.metrics if calibration else None,
        },
        "latest_parameter_version": parameter.version_tag if parameter else None,
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
