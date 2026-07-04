from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from app.services.contracts import (
    FIRST_FIVE_SPREAD,
    FIRST_FIVE_TOTAL,
    FIRST_FIVE_WINNER,
    FULL_GAME_SPREAD,
    FULL_GAME_TOTAL,
    FULL_GAME_WINNER,
    PAPER_SUPPORTED_MARKET_FAMILIES,
)
from app.services.modeling import (
    BASELINE_PARAMETER_VERSION_TAG,
    DEFAULT_MODEL_PARAMETERS,
    FEATURE_VERSION,
    MATURE_MODEL_TAG,
    ModelScore,
    RunExpectations,
    _bounded,
    _calibrate_probability,
    _decimal,
    _joint_probability,
    _poisson_distribution,
    _training_eligibility,
    expected_runs,
)


PROBABILITY_ADAPTER_POLICY_VERSION = "pr3u_family_scope_probability_adapters_v1"
PROBABILITY_ADAPTER_FEATURE_POLICY_VERSION = "mature_mlb_features_v2_probability_adapters_v1"
PROBABILITY_ADAPTER_CALIBRATION_VERSION = "shared_parameter_offsets_pre_pr3v"

ADAPTER_VERSION_BY_FAMILY = {
    FULL_GAME_TOTAL: "full_game_total_adapter_v1",
    FIRST_FIVE_TOTAL: "first_five_total_adapter_v1",
    FULL_GAME_WINNER: "full_game_winner_adapter_v1",
    FIRST_FIVE_WINNER: "first_five_winner_adapter_v1",
    FULL_GAME_SPREAD: "full_game_spread_adapter_v1",
    FIRST_FIVE_SPREAD: "first_five_spread_adapter_v1",
}

CALIBRATION_HOOK_BY_FAMILY = {
    FULL_GAME_TOTAL: "calibration_hook_full_game_total",
    FIRST_FIVE_TOTAL: "calibration_hook_first_five_total",
    FULL_GAME_WINNER: "calibration_hook_full_game_winner",
    FIRST_FIVE_WINNER: "calibration_hook_first_five_winner",
    FULL_GAME_SPREAD: "calibration_hook_full_game_spread",
    FIRST_FIVE_SPREAD: "calibration_hook_first_five_spread",
}

SCOPE_BY_FAMILY = {
    FULL_GAME_TOTAL: "full_game",
    FULL_GAME_WINNER: "full_game",
    FULL_GAME_SPREAD: "full_game",
    FIRST_FIVE_TOTAL: "first_five",
    FIRST_FIVE_WINNER: "first_five",
    FIRST_FIVE_SPREAD: "first_five",
}

FAMILY_KIND_BY_MARKET_FAMILY = {
    FULL_GAME_TOTAL: "total",
    FIRST_FIVE_TOTAL: "total",
    FULL_GAME_WINNER: "winner",
    FIRST_FIVE_WINNER: "winner",
    FULL_GAME_SPREAD: "spread",
    FIRST_FIVE_SPREAD: "spread",
}


class ProbabilityAdapter(Protocol):
    key: str
    version: str
    market_family: str
    scope: str

    def score(self, context: "ProbabilityAdapterContext") -> "ProbabilityAdapterResult":
        ...


@dataclass(frozen=True)
class ProbabilityAdapterContext:
    features: dict[str, object]
    market_type: str
    contract_side: str
    settlement_status: str | None
    parameters: dict[str, object] | None = None
    parameter_version_tag: str | None = None
    exposure_taxonomy: object | None = None
    base_model_score: ModelScore | None = None
    spread_verification: object | None = None


@dataclass(frozen=True)
class ProbabilityAdapterResult:
    probability: Decimal
    fair_value: Decimal
    probability_raw: Decimal
    probability_calibrated: Decimal
    data_quality: Decimal
    calibration_status: str
    training_eligible: bool
    training_exclusion_reason: str | None
    push_probability: Decimal
    adapter_key: str
    adapter_version: str
    adapter_family: str
    adapter_scope: str
    adapter_rationale: str
    calibration_hook: str
    calibration_version: str
    calibration_hook_status: str
    feature_policy_version: str
    model_policy_metadata: dict[str, object]
    diagnostics: dict[str, object]

    def compact_metadata(self) -> dict[str, object]:
        return {
            "adapter_key": self.adapter_key,
            "adapter_version": self.adapter_version,
            "adapter_policy_version": PROBABILITY_ADAPTER_POLICY_VERSION,
            "adapter_family": self.adapter_family,
            "adapter_scope": self.adapter_scope,
            "adapter_rationale": self.adapter_rationale,
            "calibration_hook": self.calibration_hook,
            "calibration_version": self.calibration_version,
            "calibration_hook_status": self.calibration_hook_status,
            "feature_policy_version": self.feature_policy_version,
            "model_policy_metadata": self.model_policy_metadata,
            "diagnostics": self.diagnostics,
        }


@dataclass(frozen=True)
class _FamilyProbabilityAdapter:
    key: str
    version: str
    market_family: str
    scope: str

    def score(self, context: ProbabilityAdapterContext) -> ProbabilityAdapterResult:
        return _score_family_adapter(context, self)


def _adapter_key(market_family: str) -> str:
    return f"{market_family}_probability_adapter"


ADAPTERS: dict[str, ProbabilityAdapter] = {
    family: _FamilyProbabilityAdapter(
        key=_adapter_key(family),
        version=version,
        market_family=family,
        scope=SCOPE_BY_FAMILY[family],
    )
    for family, version in ADAPTER_VERSION_BY_FAMILY.items()
}


def adapter_for_market_type(market_type: str | None) -> ProbabilityAdapter | None:
    if market_type is None:
        return None
    return ADAPTERS.get(str(market_type))


def score_probability_adapter(context: ProbabilityAdapterContext) -> ProbabilityAdapterResult:
    adapter = adapter_for_market_type(context.market_type)
    if adapter is None:
        return _unsupported_result(context)
    return adapter.score(context)


def probability_adapter_candidate_payload(result: ProbabilityAdapterResult) -> dict[str, object]:
    return {
        "probability_adapter_key": result.adapter_key,
        "probability_adapter_version": result.adapter_version,
        "probability_adapter_policy_version": PROBABILITY_ADAPTER_POLICY_VERSION,
        "probability_adapter_family": result.adapter_family,
        "probability_adapter_scope": result.adapter_scope,
        "probability_adapter_rationale": result.adapter_rationale,
        "probability_adapter_calibration_hook": result.calibration_hook,
        "probability_adapter_calibration_version": result.calibration_version,
        "probability_adapter_feature_policy_version": result.feature_policy_version,
        "probability_adapter_metadata": result.compact_metadata(),
    }


def _score_family_adapter(
    context: ProbabilityAdapterContext,
    adapter: _FamilyProbabilityAdapter,
) -> ProbabilityAdapterResult:
    parameters = context.parameters or DEFAULT_MODEL_PARAMETERS
    parameter_version_tag = context.parameter_version_tag or BASELINE_PARAMETER_VERSION_TAG
    expectations = expected_runs(context.features, parameters)
    data_quality = _decimal(context.features.get("data_quality"), Decimal("0.10")).quantize(Decimal("0.0001"))
    raw_probability, push_probability, diagnostics = _raw_probability(context, expectations)
    if context.base_model_score is not None and not diagnostics.get("ambiguous_spread_complement"):
        raw_probability, calibrated_probability, calibration_status = _probabilities_from_base_score(context.base_model_score, context.contract_side)
        push_probability = context.base_model_score.push_probability or push_probability
        diagnostics["legacy_model_score_input"] = True
    else:
        calibrated_probability, calibration_status = _calibrate_probability(
            raw_probability,
            data_quality,
            market_type=context.market_type,
            parameters=parameters,
        )
    training_eligible, training_exclusion_reason = _training_eligibility(
        context.features,
        context.market_type,
        context.settlement_status,
    )
    if context.base_model_score is not None and not diagnostics.get("ambiguous_spread_complement"):
        training_eligible = context.base_model_score.training_eligible
        training_exclusion_reason = context.base_model_score.training_exclusion_reason
    family_kind = FAMILY_KIND_BY_MARKET_FAMILY.get(context.market_type, context.market_type)
    scope_policy = (
        "full_game_bullpen_aware"
        if adapter.scope == "full_game"
        else "first_five_starter_heavy"
    )
    diagnostics.update(
        {
            "adapter_dispatch": adapter.key,
            "run_distribution_scope": adapter.scope,
            "run_mean_source": scope_policy,
            "contract_side": context.contract_side.lower(),
            "calibration_hook": CALIBRATION_HOOK_BY_FAMILY[context.market_type],
        }
    )
    model_policy_metadata = {
        "policy_version": PROBABILITY_ADAPTER_POLICY_VERSION,
        "model_version": MATURE_MODEL_TAG,
        "parameter_version": parameter_version_tag,
        "feature_version": FEATURE_VERSION,
        "family_kind": family_kind,
        "scope_policy": scope_policy,
        "calibration_mode": "shared_or_uncalibrated_until_pr3v",
        "calibration_status": calibration_status,
        "uses_market_price": False,
    }
    return ProbabilityAdapterResult(
        probability=calibrated_probability,
        probability_raw=raw_probability,
        probability_calibrated=calibrated_probability,
        fair_value=calibrated_probability.quantize(Decimal("0.0001")),
        data_quality=data_quality,
        calibration_status=calibration_status,
        training_eligible=training_eligible,
        training_exclusion_reason=training_exclusion_reason,
        push_probability=push_probability,
        adapter_key=adapter.key,
        adapter_version=adapter.version,
        adapter_family=context.market_type,
        adapter_scope=adapter.scope,
        adapter_rationale=f"{adapter.scope} {family_kind} probability adapter using mature run distribution v2",
        calibration_hook=CALIBRATION_HOOK_BY_FAMILY[context.market_type],
        calibration_version=PROBABILITY_ADAPTER_CALIBRATION_VERSION,
        calibration_hook_status="metadata_only_pending_pr3v",
        feature_policy_version=PROBABILITY_ADAPTER_FEATURE_POLICY_VERSION,
        model_policy_metadata=model_policy_metadata,
        diagnostics=diagnostics,
    )


def _probabilities_from_base_score(base_score: ModelScore, contract_side: str) -> tuple[Decimal, Decimal, str]:
    yes_raw = (base_score.probability_raw or base_score.probability).quantize(Decimal("0.000001"))
    yes_calibrated = (base_score.probability_calibrated or base_score.probability).quantize(Decimal("0.000001"))
    if contract_side.lower() == "no":
        return (
            _bounded(Decimal("1.000000") - yes_raw, Decimal("0"), Decimal("1")).quantize(Decimal("0.000001")),
            _bounded(Decimal("1.000000") - yes_calibrated, Decimal("0.020000"), Decimal("0.980000")).quantize(Decimal("0.000001")),
            base_score.calibration_status or "baseline_parameterized",
        )
    return yes_raw, yes_calibrated, base_score.calibration_status or "baseline_parameterized"


def _raw_probability(
    context: ProbabilityAdapterContext,
    expectations: RunExpectations,
) -> tuple[Decimal, Decimal, dict[str, object]]:
    scope = SCOPE_BY_FAMILY.get(context.market_type, "full_game")
    away_dist, home_dist = _run_distributions(expectations, scope)
    diagnostics: dict[str, object] = {}
    if context.market_type in {FULL_GAME_TOTAL, FIRST_FIVE_TOTAL}:
        return _total_probability(context, away_dist, home_dist)
    if context.market_type in {FULL_GAME_WINNER, FIRST_FIVE_WINNER}:
        return _winner_probability(context, away_dist, home_dist)
    if context.market_type in {FULL_GAME_SPREAD, FIRST_FIVE_SPREAD}:
        return _spread_probability(context, away_dist, home_dist)
    diagnostics["adapter_error"] = "unsupported_market_family"
    return Decimal("0.500000"), Decimal("0.000000"), diagnostics


def _run_distributions(
    expectations: RunExpectations,
    scope: str,
) -> tuple[list[Decimal], list[Decimal]]:
    if scope == "first_five":
        return (
            _poisson_distribution(expectations.away_first_five_runs_mean),
            _poisson_distribution(expectations.home_first_five_runs_mean),
        )
    return (
        _poisson_distribution(expectations.away_full_game_runs_mean),
        _poisson_distribution(expectations.home_full_game_runs_mean),
    )


def _total_probability(
    context: ProbabilityAdapterContext,
    away_dist: list[Decimal],
    home_dist: list[Decimal],
) -> tuple[Decimal, Decimal, dict[str, object]]:
    line = _line_value(context)
    direction = _taxonomy_text(context, "economic_exposure_direction") or _over_under_side(context)
    if direction not in {"over", "under"} or line is None:
        return Decimal("0.500000"), Decimal("0.000000"), {"adapter_error": "missing_total_direction_or_line"}
    if direction == "over":
        probability = _joint_probability(away_dist, home_dist, lambda away, home: Decimal(away + home) > line)
    else:
        probability = _joint_probability(away_dist, home_dist, lambda away, home: Decimal(away + home) < line)
    push_probability = _joint_probability(away_dist, home_dist, lambda away, home: Decimal(away + home) == line)
    return probability, push_probability, {"normalized_total_direction": direction, "line_value": float(line)}


def _winner_probability(
    context: ProbabilityAdapterContext,
    away_dist: list[Decimal],
    home_dist: list[Decimal],
) -> tuple[Decimal, Decimal, dict[str, object]]:
    home_code, away_code = _team_codes(context.features)
    direction = _taxonomy_text(context, "economic_exposure_direction")
    team = (_taxonomy_text(context, "economic_exposure_team") or _selected_team(context) or "").upper()
    home_win = _joint_probability(away_dist, home_dist, lambda away, home: home > away)
    away_win = _joint_probability(away_dist, home_dist, lambda away, home: away > home)
    tie = _joint_probability(away_dist, home_dist, lambda away, home: away == home)
    if context.market_type == FULL_GAME_WINNER:
        if team == home_code:
            return _conditional_no_tie(home_win, tie), tie, {"winner_team": team, "tie_policy": "excluded_full_game"}
        if team == away_code:
            return _conditional_no_tie(away_win, tie), tie, {"winner_team": team, "tie_policy": "excluded_full_game"}
        return Decimal("0.500000"), tie, {"adapter_error": "missing_full_game_winner_team"}
    if team == home_code and direction in {None, "win"}:
        return home_win, tie, {"winner_team": team, "tie_policy": "separate_first_five_tie"}
    if team == away_code and direction in {None, "win"}:
        return away_win, tie, {"winner_team": team, "tie_policy": "separate_first_five_tie"}
    if direction == "not_win":
        if team == home_code:
            return (away_win + tie).quantize(Decimal("0.000001")), tie, {"winner_team": team, "tie_policy": "tie_included_in_no_contract"}
        if team == away_code:
            return (home_win + tie).quantize(Decimal("0.000001")), tie, {"winner_team": team, "tie_policy": "tie_included_in_no_contract"}
    if direction in {"tie", "draw"} or _selected_team(context) == "TIE":
        return tie, tie, {"winner_team": "TIE", "tie_policy": "tie_contract_diagnostics_only"}
    if direction == "either_team_win":
        return (home_win + away_win).quantize(Decimal("0.000001")), tie, {"winner_team": None, "tie_policy": "no_on_tie_means_either_team_wins"}
    return Decimal("0.500000"), tie, {"adapter_error": "missing_first_five_winner_semantics"}


def _spread_probability(
    context: ProbabilityAdapterContext,
    away_dist: list[Decimal],
    home_dist: list[Decimal],
) -> tuple[Decimal, Decimal, dict[str, object]]:
    home_code, away_code = _team_codes(context.features)
    line = _taxonomy_decimal(context, "economic_exposure_line") or _line_value(context)
    team = (_taxonomy_text(context, "economic_exposure_team") or _selected_team(context) or "").upper()
    direction = _taxonomy_text(context, "economic_exposure_direction")
    if line is None or team not in {home_code, away_code}:
        return Decimal("0.500000"), Decimal("0.000000"), {"adapter_error": "missing_spread_team_or_line"}
    if direction == "not_cover":
        return Decimal("0.500000"), Decimal("0.000000"), {
            "adapter_error": "ambiguous_spread_complement",
            "ambiguous_spread_complement": True,
        }
    if team == home_code:
        probability = _joint_probability(away_dist, home_dist, lambda away, home: Decimal(home - away) + line > 0)
        push_probability = _joint_probability(away_dist, home_dist, lambda away, home: Decimal(home - away) + line == 0)
    else:
        probability = _joint_probability(away_dist, home_dist, lambda away, home: Decimal(away - home) + line > 0)
        push_probability = _joint_probability(away_dist, home_dist, lambda away, home: Decimal(away - home) + line == 0)
    return probability, push_probability, {"spread_team": team, "spread_line": float(line), "spread_direction": "cover"}


def _unsupported_result(context: ProbabilityAdapterContext) -> ProbabilityAdapterResult:
    data_quality = _decimal(context.features.get("data_quality"), Decimal("0.10")).quantize(Decimal("0.0001"))
    return ProbabilityAdapterResult(
        probability=Decimal("0.500000"),
        probability_raw=Decimal("0.500000"),
        probability_calibrated=Decimal("0.500000"),
        fair_value=Decimal("0.5000"),
        data_quality=data_quality,
        calibration_status="unsupported_adapter",
        training_eligible=False,
        training_exclusion_reason="unsupported_market_family",
        push_probability=Decimal("0.000000"),
        adapter_key="unsupported_probability_adapter",
        adapter_version="unsupported_probability_adapter_v1",
        adapter_family=str(context.market_type or "unknown"),
        adapter_scope="unknown",
        adapter_rationale="unsupported market family for PR3u probability adapters",
        calibration_hook="calibration_hook_unsupported",
        calibration_version=PROBABILITY_ADAPTER_CALIBRATION_VERSION,
        calibration_hook_status="not_applicable",
        feature_policy_version=PROBABILITY_ADAPTER_FEATURE_POLICY_VERSION,
        model_policy_metadata={
            "policy_version": PROBABILITY_ADAPTER_POLICY_VERSION,
            "model_version": MATURE_MODEL_TAG,
            "supported_families": sorted(PAPER_SUPPORTED_MARKET_FAMILIES),
        },
        diagnostics={"adapter_error": "unsupported_market_family"},
    )


def _conditional_no_tie(win_probability: Decimal, tie_probability: Decimal) -> Decimal:
    non_tie = Decimal("1.000000") - tie_probability
    if non_tie <= Decimal("0"):
        return Decimal("0.500000")
    return _bounded(win_probability / non_tie, Decimal("0"), Decimal("1")).quantize(Decimal("0.000001"))


def _market_context(features: dict[str, object]) -> dict[str, Any]:
    value = features.get("market_context")
    return value if isinstance(value, dict) else {}


def _game_context(features: dict[str, object]) -> dict[str, Any]:
    value = features.get("game_context")
    return value if isinstance(value, dict) else {}


def _team_codes(features: dict[str, object]) -> tuple[str, str]:
    context = _game_context(features)
    return (
        str(context.get("home_abbreviation") or "").upper(),
        str(context.get("away_abbreviation") or "").upper(),
    )


def _line_value(context: ProbabilityAdapterContext) -> Decimal | None:
    value = _market_context(context.features).get("line_value")
    if value is None:
        return None
    return _decimal(value).quantize(Decimal("0.0001"))


def _over_under_side(context: ProbabilityAdapterContext) -> str | None:
    value = _market_context(context.features).get("over_under_side")
    return str(value).lower() if value else None


def _selected_team(context: ProbabilityAdapterContext) -> str | None:
    value = _market_context(context.features).get("selection_code")
    return str(value).upper() if value else None


def _taxonomy_value(context: ProbabilityAdapterContext, key: str) -> object | None:
    taxonomy = context.exposure_taxonomy
    if taxonomy is None:
        taxonomy = _market_context(context.features).get("exposure_taxonomy") or context.features.get("exposure_taxonomy")
    if isinstance(taxonomy, dict):
        return taxonomy.get(key)
    return getattr(taxonomy, key, None)


def _taxonomy_text(context: ProbabilityAdapterContext, key: str) -> str | None:
    value = _taxonomy_value(context, key)
    return str(value).lower() if value is not None else None


def _taxonomy_decimal(context: ProbabilityAdapterContext, key: str) -> Decimal | None:
    value = _taxonomy_value(context, key)
    if value is None:
        return None
    return _decimal(value).quantize(Decimal("0.0001"))
