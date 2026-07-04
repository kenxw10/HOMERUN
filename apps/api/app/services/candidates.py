from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    FeatureSnapshot,
    KalshiMarket,
    MarketMapping,
    MlbGame,
    ModelCandidate,
    ModelPredictionOutput,
    ModelPredictionRun,
    PaperTrade,
)
from app.services.contracts import (
    FIRST_FIVE_SPREAD,
    FIRST_FIVE_TOTAL,
    FIRST_FIVE_WINNER,
    FULL_GAME_SPREAD,
    FULL_GAME_TOTAL,
    FULL_GAME_WINNER,
    PAPER_SUPPORTED_MARKET_FAMILIES,
    contract_labels,
    game_team_codes,
    has_trusted_selection,
    market_type_from_ticker,
)
from app.services.exposure_taxonomy import (
    EXPOSURE_TAXONOMY_VERSION,
    ExposureTaxonomy,
    LINE_CLASSIFICATION_POLICY_VERSION,
    LineClassification,
    candidate_line_ladder_key,
    exposure_taxonomy_for_candidate,
    line_classification_for_ladder,
)
from app.services.features import CORE_MODULES, FEATURE_VERSION, QUALITY_WEIGHTS, build_feature_snapshot
from app.services.live_like_selector import (
    SELECTOR_POLICY_VERSION,
    apply_live_like_selector,
    selector_metadata_payload,
)
from app.services.mapping import infer_market_type
from app.services.modeling import (
    MATURE_MODEL_TAG,
    get_or_create_active_parameter_version,
    get_or_create_mature_model_version,
    score_mature_candidate,
)
from app.services.probability_adapters import (
    PROBABILITY_ADAPTER_POLICY_VERSION,
    ProbabilityAdapterContext,
    probability_adapter_candidate_payload,
    score_probability_adapter,
)
from app.services.portfolio import calculate_paper_portfolio, create_balance_snapshot, paper_trade_fee
from app.services.paper_epoch import get_or_create_active_paper_epoch
from app.services.spread_verification import (
    SPREAD_FAMILIES,
    SpreadVerification,
    spread_verification_from_cached_metadata,
    spread_verification_from_mapping,
)
from app.time_utils import classify_time_bucket, ensure_aware_utc, get_dashboard_zone, utc_now

TRADABLE_MARKET_STATUSES = {"active", "open"}
PLAYABLE_GAME_STATUSES = {"pre-game", "preview", "scheduled", "warmup"}
SELECTION_REQUIRED_FAMILIES = {"full_game_winner", "full_game_spread", "first_five_winner", "first_five_spread"}
FULL_GAME_SPREAD_TRUSTED_AUDIT_STATUS = "trusted_audit_only"
FULL_GAME_SPREAD_AUDIT_REASON_BY_STATUS = {
    "parse_error": "no_trade_full_game_spread_parse_error",
    "unsafe": "no_trade_full_game_spread_unsafe",
    "needs_review": "no_trade_full_game_spread_needs_review",
    "missing_market_data": "no_trade_full_game_spread_audit_missing",
    "missing_game_mapping": "no_trade_full_game_spread_audit_missing",
    "missing_line": "no_trade_full_game_spread_audit_missing",
}
COMPACT_SPREAD_AUDIT_KEYS = (
    "family_key",
    "parser_status",
    "settlement_rule_status",
    "verified",
    "paper_trade_allowed_if_enabled",
    "audit_status",
    "reason_codes",
    "selection_code",
    "line_value",
    "inning_scope",
    "actual_contract_display",
    "no_contract_display",
    "normalized_no_equivalent_display",
    "parse_source",
    "yes_interpretation",
    "no_interpretation",
    "no_is_true_complement",
    "complement_safe_for_paper_settlement",
    "line_sign",
    "line_direction",
    "push_possible",
    "push_condition",
    "push_rule_verified",
    "condition_type",
    "threshold_runs",
    "selected_team_margin_required_gt",
    "display_spread_line",
    "settlement_formula",
    "no_text_source",
    "no_complement_source",
    "no_complement_confidence",
)

PROBABILITY_ADAPTER_CANDIDATE_FIELDS = (
    "probability_adapter_key",
    "probability_adapter_version",
    "probability_adapter_policy_version",
    "probability_adapter_family",
    "probability_adapter_scope",
    "probability_adapter_rationale",
    "probability_adapter_calibration_hook",
    "probability_adapter_calibration_version",
    "probability_adapter_feature_policy_version",
)


@dataclass
class TradeIntent:
    candidate: ModelCandidate
    game: MlbGame
    market: KalshiMarket
    price: Decimal
    labels: object
    score: Decimal
    quantity: int = 1
    sizing: dict[str, Decimal | int | str | None] | None = None


@dataclass(frozen=True)
class PriceContext:
    market_price: Decimal | None
    executable_price: Decimal | None
    source: str | None
    updated_at: datetime | None
    staleness_seconds: int | None
    status: str
    side: str


@dataclass(frozen=True)
class SizingContext:
    bankroll_at_entry: Decimal | None
    risk_pct: Decimal | None
    risk_dollars: Decimal | None
    contracts: int
    estimated_cost_per_contract: Decimal | None
    estimated_total_cost: Decimal | None
    one_contract_expected_value: Decimal | None
    sized_expected_value: Decimal | None
    one_contract_fee_estimate: Decimal | None
    total_fee_estimate: Decimal | None
    status: str

    def as_dict(self) -> dict[str, object]:
        return {
            "bankroll_at_entry": float(self.bankroll_at_entry) if self.bankroll_at_entry is not None else None,
            "risk_pct": float(self.risk_pct) if self.risk_pct is not None else None,
            "risk_dollars": float(self.risk_dollars) if self.risk_dollars is not None else None,
            "contracts": self.contracts,
            "estimated_cost_per_contract": (
                float(self.estimated_cost_per_contract) if self.estimated_cost_per_contract is not None else None
            ),
            "estimated_total_cost": float(self.estimated_total_cost) if self.estimated_total_cost is not None else None,
            "one_contract_expected_value": (
                float(self.one_contract_expected_value) if self.one_contract_expected_value is not None else None
            ),
            "sized_expected_value": float(self.sized_expected_value) if self.sized_expected_value is not None else None,
            "one_contract_fee_estimate": (
                float(self.one_contract_fee_estimate) if self.one_contract_fee_estimate is not None else None
            ),
            "total_fee_estimate": float(self.total_fee_estimate) if self.total_fee_estimate is not None else None,
            "status": self.status,
        }


def _apply_exposure_taxonomy(candidate: ModelCandidate, taxonomy: ExposureTaxonomy) -> None:
    candidate.economic_exposure_label = taxonomy.economic_exposure_label
    candidate.economic_exposure_key = taxonomy.economic_exposure_key
    candidate.economic_exposure_family = taxonomy.economic_exposure_family
    candidate.economic_exposure_scope = taxonomy.economic_exposure_scope
    candidate.economic_exposure_direction = taxonomy.economic_exposure_direction
    candidate.economic_exposure_team = taxonomy.economic_exposure_team
    candidate.economic_exposure_line = taxonomy.economic_exposure_line
    candidate.contract_mechanics_label = taxonomy.contract_mechanics_label
    candidate.concept_cluster_key = taxonomy.concept_cluster_key
    candidate.same_game_concept_cluster_key = taxonomy.same_game_concept_cluster_key
    candidate.exposure_taxonomy_version = taxonomy.exposure_taxonomy_version


def _apply_line_classification(candidate: ModelCandidate, classification: LineClassification) -> None:
    candidate.line_class = classification.line_class
    candidate.line_class_reason = classification.line_class_reason
    candidate.line_ladder_rank = classification.line_ladder_rank
    candidate.line_ladder_distance_from_central = classification.line_ladder_distance_from_central
    candidate.line_ladder_size = classification.line_ladder_size
    candidate.line_classification_policy_version = classification.line_classification_policy_version


def _copy_exposure_metadata_to_trade(trade: PaperTrade, candidate: ModelCandidate) -> None:
    trade.economic_exposure_label = candidate.economic_exposure_label
    trade.economic_exposure_key = candidate.economic_exposure_key
    trade.economic_exposure_family = candidate.economic_exposure_family
    trade.economic_exposure_scope = candidate.economic_exposure_scope
    trade.economic_exposure_direction = candidate.economic_exposure_direction
    trade.economic_exposure_team = candidate.economic_exposure_team
    trade.economic_exposure_line = candidate.economic_exposure_line
    trade.contract_mechanics_label = candidate.contract_mechanics_label
    trade.concept_cluster_key = candidate.concept_cluster_key
    trade.same_game_concept_cluster_key = candidate.same_game_concept_cluster_key
    trade.line_class = candidate.line_class
    trade.line_class_reason = candidate.line_class_reason
    trade.line_ladder_rank = candidate.line_ladder_rank
    trade.line_ladder_distance_from_central = candidate.line_ladder_distance_from_central
    trade.line_ladder_size = candidate.line_ladder_size
    trade.exposure_taxonomy_version = candidate.exposure_taxonomy_version
    trade.line_classification_policy_version = candidate.line_classification_policy_version


def _copy_selector_metadata_to_trade(trade: PaperTrade, candidate: ModelCandidate) -> None:
    trade.selector_policy_version = candidate.selector_policy_version
    trade.selector_mode = candidate.selector_mode
    trade.selector_status = candidate.selector_status
    trade.selector_decision = candidate.selector_decision
    trade.selector_rejection_reason = candidate.selector_rejection_reason
    trade.selector_threshold_profile = candidate.selector_threshold_profile
    trade.selector_min_net_ev = candidate.selector_min_net_ev
    trade.selector_min_prob_edge = candidate.selector_min_prob_edge
    trade.selector_min_data_quality = candidate.selector_min_data_quality
    trade.selector_line_class_policy = candidate.selector_line_class_policy
    trade.selector_concept_cluster_key = candidate.selector_concept_cluster_key
    trade.selector_same_game_concept_cluster_key = candidate.selector_same_game_concept_cluster_key
    trade.selector_cluster_rank = candidate.selector_cluster_rank
    trade.selector_cluster_rank_score = candidate.selector_cluster_rank_score
    trade.selector_selected_from_cluster = candidate.selector_selected_from_cluster
    trade.selector_shadow_only = candidate.selector_shadow_only
    trade.selector_live_like_eligible_before_cluster = candidate.selector_live_like_eligible_before_cluster
    trade.selector_live_like_eligible_after_cluster = candidate.selector_live_like_eligible_after_cluster


def _candidate_exposure_payload(candidate: ModelCandidate) -> dict[str, object]:
    return {
        "economic_exposure_label": candidate.economic_exposure_label,
        "economic_exposure_key": candidate.economic_exposure_key,
        "economic_exposure_family": candidate.economic_exposure_family,
        "economic_exposure_scope": candidate.economic_exposure_scope,
        "economic_exposure_direction": candidate.economic_exposure_direction,
        "economic_exposure_team": candidate.economic_exposure_team,
        "economic_exposure_line": (
            str(candidate.economic_exposure_line) if candidate.economic_exposure_line is not None else None
        ),
        "contract_mechanics_label": candidate.contract_mechanics_label,
        "concept_cluster_key": candidate.concept_cluster_key,
        "same_game_concept_cluster_key": candidate.same_game_concept_cluster_key,
        "line_class": candidate.line_class,
        "line_class_reason": candidate.line_class_reason,
        "line_ladder_rank": candidate.line_ladder_rank,
        "line_ladder_distance_from_central": candidate.line_ladder_distance_from_central,
        "line_ladder_size": candidate.line_ladder_size,
        "exposure_taxonomy_version": candidate.exposure_taxonomy_version,
        "line_classification_policy_version": candidate.line_classification_policy_version,
    }


def _candidate_exposure_field_counts(candidates: list[ModelCandidate]) -> dict[str, int]:
    counts = {
        "economic_exposure_label": 0,
        "economic_exposure_key": 0,
        "economic_exposure_family": 0,
        "economic_exposure_scope": 0,
        "economic_exposure_direction": 0,
        "economic_exposure_team": 0,
        "economic_exposure_line": 0,
        "contract_mechanics_label": 0,
        "concept_cluster_key": 0,
        "same_game_concept_cluster_key": 0,
        "line_class": 0,
        "line_class_reason": 0,
        "line_ladder_rank": 0,
        "line_ladder_distance_from_central": 0,
        "line_ladder_size": 0,
        "exposure_taxonomy_version": 0,
        "line_classification_policy_version": 0,
    }
    for candidate in candidates:
        payload = _candidate_exposure_payload(candidate)
        for field, value in payload.items():
            if value is not None:
                counts[field] += 1
    return counts


def _candidate_selector_field_counts(candidates: list[ModelCandidate]) -> dict[str, int]:
    counts = {
        "selector_policy_version": 0,
        "selector_mode": 0,
        "selector_status": 0,
        "selector_decision": 0,
        "selector_rejection_reason": 0,
        "selector_threshold_profile": 0,
        "selector_min_net_ev": 0,
        "selector_min_prob_edge": 0,
        "selector_min_data_quality": 0,
        "selector_line_class_policy": 0,
        "selector_concept_cluster_key": 0,
        "selector_same_game_concept_cluster_key": 0,
        "selector_cluster_rank": 0,
        "selector_cluster_rank_score": 0,
        "selector_selected_from_cluster": 0,
        "selector_shadow_only": 0,
        "selector_live_like_eligible_before_cluster": 0,
        "selector_live_like_eligible_after_cluster": 0,
    }
    for candidate in candidates:
        payload = selector_metadata_payload(candidate)
        for field, value in payload.items():
            if value is not None:
                counts[field] += 1
    return counts


def _apply_probability_adapter_metadata(candidate: ModelCandidate, payload: dict[str, object]) -> None:
    candidate.probability_adapter_key = payload.get("probability_adapter_key")
    candidate.probability_adapter_version = payload.get("probability_adapter_version")
    candidate.probability_adapter_policy_version = payload.get("probability_adapter_policy_version")
    candidate.probability_adapter_family = payload.get("probability_adapter_family")
    candidate.probability_adapter_scope = payload.get("probability_adapter_scope")
    candidate.probability_adapter_rationale = payload.get("probability_adapter_rationale")
    candidate.probability_adapter_calibration_hook = payload.get("probability_adapter_calibration_hook")
    candidate.probability_adapter_calibration_version = payload.get("probability_adapter_calibration_version")
    candidate.probability_adapter_feature_policy_version = payload.get("probability_adapter_feature_policy_version")
    candidate.probability_adapter_metadata = payload.get("probability_adapter_metadata")


def _candidate_probability_adapter_payload(candidate: ModelCandidate) -> dict[str, object]:
    return {
        "probability_adapter_key": candidate.probability_adapter_key,
        "probability_adapter_version": candidate.probability_adapter_version,
        "probability_adapter_policy_version": candidate.probability_adapter_policy_version,
        "probability_adapter_family": candidate.probability_adapter_family,
        "probability_adapter_scope": candidate.probability_adapter_scope,
        "probability_adapter_rationale": candidate.probability_adapter_rationale,
        "probability_adapter_calibration_hook": candidate.probability_adapter_calibration_hook,
        "probability_adapter_calibration_version": candidate.probability_adapter_calibration_version,
        "probability_adapter_feature_policy_version": candidate.probability_adapter_feature_policy_version,
    }


def _candidate_probability_adapter_field_counts(candidates: list[ModelCandidate]) -> dict[str, int]:
    counts = {field: 0 for field in PROBABILITY_ADAPTER_CANDIDATE_FIELDS}
    for candidate in candidates:
        payload = _candidate_probability_adapter_payload(candidate)
        for field, value in payload.items():
            if value is not None:
                counts[field] += 1
    return counts


def _probability_adapter_summary(candidates: list[ModelCandidate]) -> dict[str, object]:
    adapter_counts: dict[str, int] = {}
    hook_counts: dict[str, int] = {}
    family_counts: dict[str, int] = {}
    error_reason_counts: dict[str, int] = {}
    error_family_counts: dict[str, int] = {}
    missing_count = 0
    error_count = 0
    for candidate in candidates:
        key = candidate.probability_adapter_key
        version = candidate.probability_adapter_version
        hook = candidate.probability_adapter_calibration_hook
        family = candidate.probability_adapter_family
        if key and version:
            adapter_counts[f"{key}:{version}"] = adapter_counts.get(f"{key}:{version}", 0) + 1
        else:
            missing_count += 1
        if hook:
            hook_counts[hook] = hook_counts.get(hook, 0) + 1
        if family:
            family_counts[family] = family_counts.get(family, 0) + 1
        metadata = candidate.probability_adapter_metadata if isinstance(candidate.probability_adapter_metadata, dict) else {}
        diagnostics = metadata.get("diagnostics") if isinstance(metadata.get("diagnostics"), dict) else {}
        adapter_error = diagnostics.get("adapter_error")
        if adapter_error:
            error_count += 1
            reason = str(adapter_error)
            error_reason_counts[reason] = error_reason_counts.get(reason, 0) + 1
            error_family = str(family or candidate.market_family or candidate.market_type or "unknown")
            error_family_counts[error_family] = error_family_counts.get(error_family, 0) + 1
    return {
        "probability_adapter_policy_version": PROBABILITY_ADAPTER_POLICY_VERSION,
        "probability_adapter_counts": adapter_counts,
        "probability_adapter_calibration_hook_counts": hook_counts,
        "probability_adapter_family_counts": family_counts,
        "probability_adapter_missing_count": missing_count,
        "probability_adapter_error_count": error_count,
        "probability_adapter_error_reason_counts": error_reason_counts,
        "probability_adapter_error_family_counts": error_family_counts,
        "probability_adapter_errors_excluded_from_governance_training": True,
    }


def _apply_candidate_line_classifications(
    candidates: list[ModelCandidate],
    outputs_by_candidate_id: dict[int, ModelPredictionOutput],
) -> dict[str, int]:
    grouped_lines: dict[tuple[object, ...], list[Decimal | None]] = {}
    for candidate in candidates:
        key = candidate_line_ladder_key(candidate)
        if key is not None:
            grouped_lines.setdefault(key, []).append(candidate.line_value)

    counts: dict[str, int] = {}
    for candidate in candidates:
        key = candidate_line_ladder_key(candidate)
        classification = line_classification_for_ladder(
            market_family=candidate.market_family or candidate.market_type,
            line_value=candidate.line_value,
            ladder_lines=grouped_lines.get(key, []),
        )
        _apply_line_classification(candidate, classification)
        counts[classification.line_class] = counts.get(classification.line_class, 0) + 1

        payload = _candidate_exposure_payload(candidate)
        candidate.features = {**(candidate.features or {}), "exposure_taxonomy": payload}
        candidate.scoring_rationale = {**(candidate.scoring_rationale or {}), "exposure_taxonomy": payload}
        diagnostics = dict(candidate.gate_diagnostics or {})
        diagnostics["exposure_taxonomy"] = payload
        candidate.gate_diagnostics = diagnostics

        if candidate.id is not None:
            output = outputs_by_candidate_id.get(candidate.id)
            if output is not None:
                raw = dict(output.raw_output or {})
                raw["exposure_taxonomy"] = payload
                gate_diagnostics = dict(raw.get("gate_diagnostics") or {})
                gate_diagnostics["exposure_taxonomy"] = payload
                raw["gate_diagnostics"] = gate_diagnostics
                output.raw_output = raw
    return counts


@dataclass(frozen=True)
class QualityContext:
    raw_feature_snapshot_data_quality: Decimal | None
    paper_observation_data_quality: Decimal
    threshold: Decimal
    candidate_stage_market_context: dict[str, object]
    decomposition: dict[str, object]
    quality_block_reason: list[str]


SWEEP_CLASSIFICATIONS = (
    "in_window",
    "excluded_too_soon",
    "excluded_too_late",
    "excluded_started",
    "excluded_wrong_date",
)

PAPER_OBSERVATION_QUALITY_WEIGHTS: dict[str, Decimal] = {
    "game_context": Decimal("0.08"),
    "market_context": Decimal("0.12"),
    "team_strength_prior": Decimal("0.12"),
    "offense_season": Decimal("0.12"),
    "offense_recent": Decimal("0.10"),
    "handedness_platoon": Decimal("0.07"),
    "starter_identity": Decimal("0.12"),
    "starter_season": Decimal("0.10"),
    "starter_recent": Decimal("0.08"),
    "starter_workload": Decimal("0.05"),
    "park_weather": Decimal("0.04"),
}

QUALITY_MODULE_ROLES: dict[str, str] = {
    "game_context": "core",
    "market_context": "candidate_stage",
    "team_strength_prior": "core",
    "offense_season": "core",
    "offense_recent": "core",
    "handedness_platoon": "core",
    "starter_identity": "core",
    "starter_season": "core",
    "starter_recent": "core",
    "starter_workload": "core",
    "park_weather": "core",
    "bullpen_season": "supporting",
    "bullpen_recent_workload": "optional_structural",
    "lineup": "supporting",
    "injuries": "optional_structural",
    "defense_catcher": "optional_structural",
    "travel_schedule": "optional_structural",
}

QUALITY_STATUS_ORDER = {
    "available": 3,
    "partial": 2,
    "missing": 1,
    "unavailable": 0,
}


def _candidate_day_bounds(now: datetime, target_date: date | None = None) -> tuple[date, datetime, datetime]:
    dashboard_zone = get_dashboard_zone()
    day = target_date or now.astimezone(dashboard_zone).date()
    day_start = ensure_aware_utc(datetime.combine(day, time.min, tzinfo=dashboard_zone))
    return day, day_start, day_start + timedelta(days=1)


def _eastern_date(value: datetime) -> date:
    return ensure_aware_utc(value).astimezone(get_dashboard_zone()).date()


def _validate_sweep_window(min_minutes: int | None, max_minutes: int | None) -> None:
    if min_minutes is not None and min_minutes < 0:
        raise ValueError("min_time_to_start_minutes must be greater than or equal to 0.")
    if max_minutes is not None and max_minutes < 0:
        raise ValueError("max_time_to_start_minutes must be greater than or equal to 0.")
    if min_minutes is not None and max_minutes is not None and min_minutes > max_minutes:
        raise ValueError("min_time_to_start_minutes must be less than or equal to max_time_to_start_minutes.")


def _sweep_classification(
    game: MlbGame,
    *,
    target_date: date,
    now: datetime,
    min_minutes: int | None,
    max_minutes: int | None,
) -> tuple[str, int]:
    minutes_to_start = int((ensure_aware_utc(game.scheduled_start) - ensure_aware_utc(now)).total_seconds() / 60)
    if _eastern_date(game.scheduled_start) != target_date:
        return "excluded_wrong_date", minutes_to_start
    if minutes_to_start <= 0:
        return "excluded_started", minutes_to_start
    if min_minutes is not None and minutes_to_start < min_minutes:
        return "excluded_too_soon", minutes_to_start
    if max_minutes is not None and minutes_to_start > max_minutes:
        return "excluded_too_late", minutes_to_start
    return "in_window", minutes_to_start


def _eastern_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return ensure_aware_utc(value).astimezone(get_dashboard_zone()).isoformat()


def _sweep_window_summary(
    session: Session,
    *,
    target_date: date,
    day_start: datetime,
    day_end: datetime,
    now: datetime,
    min_minutes: int | None,
    max_minutes: int | None,
    sweep_label: str | None,
    dry_run_candidates_only: bool,
) -> tuple[dict[str, object], set[int]]:
    target_games = list(
        session.scalars(
            select(MlbGame)
            .where(MlbGame.scheduled_start >= day_start)
            .where(MlbGame.scheduled_start < day_end)
            .order_by(MlbGame.scheduled_start.asc(), MlbGame.id.asc())
        )
    )
    wrong_date_mapped_games = list(
        session.scalars(
            select(MlbGame)
            .join(MarketMapping, MarketMapping.mlb_game_id == MlbGame.id)
            .where(MarketMapping.mapping_status.in_(["candidate", "confirmed", "needs_review"]))
            .where(
                (MlbGame.scheduled_start >= day_start - timedelta(days=1))
                & (MlbGame.scheduled_start < day_end + timedelta(days=1))
            )
            .where((MlbGame.scheduled_start < day_start) | (MlbGame.scheduled_start >= day_end))
            .order_by(MlbGame.scheduled_start.asc(), MlbGame.id.asc())
        )
    )
    games_by_id = {game.id: game for game in target_games if game.id is not None}
    for game in wrong_date_mapped_games:
        if game.id is not None:
            games_by_id.setdefault(game.id, game)

    counts = {status: 0 for status in SWEEP_CLASSIFICATIONS}
    in_window_ids: set[int] = set()
    next_in_window: datetime | None = None
    next_too_late: datetime | None = None
    for game in games_by_id.values():
        status, _minutes = _sweep_classification(
            game,
            target_date=target_date,
            now=now,
            min_minutes=min_minutes,
            max_minutes=max_minutes,
        )
        counts[status] += 1
        if status == "in_window" and game.id is not None:
            in_window_ids.add(game.id)
            if next_in_window is None or ensure_aware_utc(game.scheduled_start) < next_in_window:
                next_in_window = ensure_aware_utc(game.scheduled_start)
        elif status == "excluded_too_late":
            if next_too_late is None or ensure_aware_utc(game.scheduled_start) < next_too_late:
                next_too_late = ensure_aware_utc(game.scheduled_start)

    summary = {
        "sweep_label": sweep_label,
        "sweep_window_enabled": min_minutes is not None or max_minutes is not None,
        "min_time_to_start_minutes": min_minutes,
        "max_time_to_start_minutes": max_minutes,
        "dry_run_candidates_only": dry_run_candidates_only,
        "sweep_started_at": now.isoformat(),
        "games_total_for_date": len([game for game in games_by_id.values() if _eastern_date(game.scheduled_start) == target_date]),
        "games_in_window": counts["in_window"],
        "games_excluded_too_soon": counts["excluded_too_soon"],
        "games_excluded_too_late": counts["excluded_too_late"],
        "games_excluded_started": counts["excluded_started"],
        "games_excluded_wrong_date": counts["excluded_wrong_date"],
        "next_game_in_window_start_time_et": _eastern_iso(next_in_window),
        "next_excluded_too_late_start_time_et": _eastern_iso(next_too_late),
    }
    return summary, in_window_ids


def _quantized(value: Decimal, places: str = "0.000001") -> Decimal:
    return value.quantize(Decimal(places))


def _is_executable_price(value: Decimal | None) -> bool:
    return value is not None and Decimal("0") < value < Decimal("1")


WS_PRICE_UPDATED_AT_KEY = "websocket_price_updated_at"
PRICE_SOURCE_TIMESTAMP_ATTRS = {
    "yes_ask": "yes_ask",
    "orderbook_implied_yes_ask": "implied_yes_ask",
    "orderbook_best_no_bid_inverse": "best_no_bid",
    "no_ask": "no_ask",
    "orderbook_implied_no_ask": "implied_no_ask",
    "orderbook_best_yes_bid_inverse": "best_yes_bid",
    "last_price_fallback": "last_price",
}


def _parse_price_timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return ensure_aware_utc(value)
    if isinstance(value, str):
        try:
            return ensure_aware_utc(datetime.fromisoformat(value))
        except ValueError:
            return None
    return None


def _websocket_price_timestamp(market: KalshiMarket, source: str | None) -> datetime | None:
    if market.market_data_source != "websocket" or source is None:
        return None
    attr = PRICE_SOURCE_TIMESTAMP_ATTRS.get(source)
    raw = market.orderbook_raw if isinstance(market.orderbook_raw, dict) else {}
    timestamps = raw.get(WS_PRICE_UPDATED_AT_KEY)
    if attr is None or not isinstance(timestamps, dict):
        return None
    return _parse_price_timestamp(timestamps.get(attr))


def _market_price_timestamp(market: KalshiMarket, now: datetime, source: str | None = None) -> datetime:
    websocket_timestamp = _websocket_price_timestamp(market, source)
    if websocket_timestamp is not None:
        return websocket_timestamp
    value = market.market_price_updated_at or market.updated_at or now
    return ensure_aware_utc(value)


def _market_yes_price(market: KalshiMarket, now: datetime | None = None) -> Decimal | None:
    return _market_yes_price_context(market, now or utc_now()).executable_price


def _market_yes_price_context(market: KalshiMarket, now: datetime) -> PriceContext:
    return _market_side_price_context(market, "yes", now)


def _open_trade_mark_price(market: KalshiMarket, side: str) -> Decimal | None:
    if side.strip().lower() == "yes":
        for value in (market.best_yes_bid, market.yes_bid, market.last_price):
            if value is not None:
                return value.quantize(Decimal("0.0001"))
    else:
        for value in (market.best_no_bid, market.no_bid):
            if value is not None:
                return value.quantize(Decimal("0.0001"))
        if market.last_price is not None:
            complement = (Decimal("1.0000") - market.last_price).quantize(Decimal("0.0001"))
            if complement >= Decimal("0"):
                return complement
    return None


def _market_side_price_context(market: KalshiMarket, side: str, now: datetime) -> PriceContext:
    settings = get_settings()
    normalized_side = side.strip().lower()
    if normalized_side not in {"yes", "no"}:
        raise ValueError(f"Unsupported contract side: {side}")
    if normalized_side == "yes":
        candidates: tuple[tuple[str, Decimal | None], ...] = (
            ("yes_ask", market.yes_ask),
            ("orderbook_implied_yes_ask", market.implied_yes_ask),
            (
                "orderbook_best_no_bid_inverse",
                (Decimal("1.0000") - market.best_no_bid) if market.best_no_bid is not None else None,
            ),
        )
    else:
        candidates = (
            ("no_ask", market.no_ask),
            ("orderbook_implied_no_ask", market.implied_no_ask),
            (
                "orderbook_best_yes_bid_inverse",
                (Decimal("1.0000") - market.best_yes_bid) if market.best_yes_bid is not None else None,
            ),
        )
    for source, value in candidates:
        if value is None:
            continue
        updated_at = _market_price_timestamp(market, now, source)
        staleness = max(0, int((ensure_aware_utc(now) - updated_at).total_seconds()))
        price = value.quantize(Decimal("0.0001"))
        if not _is_executable_price(price):
            return PriceContext(price, None, source, updated_at, staleness, "non_executable", normalized_side)
        if staleness > settings.paper_max_price_staleness_seconds:
            return PriceContext(price, None, source, updated_at, staleness, "stale", normalized_side)
        return PriceContext(price, price, source, updated_at, staleness, "fresh_executable", normalized_side)

    if normalized_side == "yes" and market.last_price is not None:
        price = market.last_price.quantize(Decimal("0.0001"))
        source = "last_price_fallback"
        updated_at = _market_price_timestamp(market, now, source)
        staleness = max(0, int((ensure_aware_utc(now) - updated_at).total_seconds()))
        if settings.paper_allow_last_price_fallback_for_trade and _is_executable_price(price):
            status = "fresh_executable" if staleness <= settings.paper_max_price_staleness_seconds else "stale"
            executable = price if status == "fresh_executable" else None
            return PriceContext(price, executable, source, updated_at, staleness, status, normalized_side)
        return PriceContext(price, None, source, updated_at, staleness, "non_executable", normalized_side)

    updated_at = _market_price_timestamp(market, now)
    staleness = max(0, int((ensure_aware_utc(now) - updated_at).total_seconds()))
    return PriceContext(None, None, None, updated_at, staleness, "missing", normalized_side)


def _round_up(value: Decimal, step: Decimal) -> Decimal:
    if value <= Decimal("0"):
        return Decimal("0.000000")
    return ((value / step).to_integral_value(rounding=ROUND_CEILING) * step).quantize(Decimal("0.000001"))


def _fee_rounding_step() -> Decimal:
    mode = get_settings().kalshi_fee_rounding_mode.strip().lower()
    if mode in {"centicent", "centicent_only", "0.0001"}:
        return Decimal("0.0001")
    return Decimal("0.01")


def _estimate_trade_fee(price: Decimal | None, quantity: int) -> Decimal | None:
    settings = get_settings()
    if not _is_executable_price(price):
        return None
    raw_fee = settings.kalshi_trade_fee_rate * Decimal(quantity) * price * (Decimal("1") - price)
    return _round_up(raw_fee, _fee_rounding_step())


def _expected_values(
    probability: Decimal | None,
    price: Decimal | None,
    quantity: int,
) -> tuple[Decimal | None, Decimal | None, Decimal | None, Decimal | None]:
    if probability is None or not _is_executable_price(price):
        return None, None, None, None
    fee = _estimate_trade_fee(price, quantity)
    if fee is None:
        return None, None, None, None
    qty = Decimal(quantity)
    # Expanded for auditability: q * (P(win) * payout_profit - P(loss) * stake).
    gross = qty * ((probability * (Decimal("1") - price)) - ((Decimal("1") - probability) * price))
    gross = _quantized(gross)
    edge = _quantized(probability - price)
    net = _quantized(gross - fee)
    return gross, fee, net, edge


def _decimal_json(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _quality_decimal(value: object, default: Decimal = Decimal("0")) -> Decimal:
    if value is None:
        return default
    try:
        return Decimal(str(value))
    except Exception:
        return default


def _module_status_from_features(features: dict[str, object], module_name: str) -> str:
    source_statuses = features.get("source_statuses")
    value: object = None
    if isinstance(source_statuses, dict):
        value = source_statuses.get(module_name)
    if value is None:
        module = features.get(module_name)
        if isinstance(module, dict):
            value = module.get("source_status")

    if isinstance(value, dict):
        statuses = [str(item or "missing").lower() for item in value.values()]
        if statuses and all(status == "available" for status in statuses):
            return "available"
        if any(status in {"available", "partial"} for status in statuses):
            return "partial"
        if any(status == "unavailable" for status in statuses):
            return "unavailable"
        return "missing"

    status = str(value or "missing").lower()
    return status if status in QUALITY_STATUS_ORDER else "missing"


def _status_score(status: str, completeness: Decimal | None = None) -> Decimal:
    completeness = completeness if completeness is not None else Decimal("0")
    if status == "available":
        return min(max(completeness, Decimal("0")), Decimal("1"))
    if status == "partial":
        return min(max(completeness, Decimal("0")), Decimal("0.5"))
    return Decimal("0")


def _candidate_stage_market_context(
    *,
    mapping: MarketMapping,
    game: MlbGame,
    market: KalshiMarket,
    market_type: str,
    price_context: PriceContext,
    contract_side: str,
) -> dict[str, object]:
    settings = get_settings()
    settlement_status = _settlement_status(mapping, market)
    selection_code = (mapping.selection_code or market.selection_code or "").upper()
    f5_tie_enabled = not (market_type == FIRST_FIVE_WINNER and selection_code == "TIE")
    checks = {
        "mapping_trusted": mapping.mapping_status != "needs_review"
        and (mapping.confidence or Decimal("0")) >= Decimal("0.55"),
        "market_family_supported": market_type in PAPER_SUPPORTED_MARKET_FAMILIES,
        "settlement_supported": market_type == FULL_GAME_WINNER or settlement_status == "paper_supported",
        "f5_tie_enabled": f5_tie_enabled,
        "selection_trusted": (
            not settings.safe_execution_posture
            or market_type not in SELECTION_REQUIRED_FAMILIES
            or _has_trusted_candidate_selection(mapping, game, market)
        ),
        "market_open": market.status.strip().lower() in TRADABLE_MARKET_STATUSES,
        "price_fresh_executable": price_context.status == "fresh_executable",
        "side_known": contract_side.lower() in {"yes", "no"},
    }
    passed = sum(1 for value in checks.values() if value)
    failed = [key for key, value in checks.items() if not value]
    if passed == len(checks):
        status = "available"
        completeness = Decimal("1.0000")
        confidence = Decimal("0.9500")
    elif passed:
        status = "partial"
        completeness = min(Decimal("0.5000"), (Decimal(passed) / Decimal(len(checks))).quantize(Decimal("0.0001")))
        confidence = completeness
    else:
        status = "missing"
        completeness = Decimal("0.0000")
        confidence = Decimal("0.0000")
    return {
        "source_status": status,
        "role": "candidate_stage",
        "source": "candidate_engine_market_context",
        "confidence": float(confidence),
        "completeness": float(completeness),
        "checks": checks,
        "failed_checks": failed,
        "contract_side": contract_side,
        "market_family": market_type,
        "settlement_status": settlement_status,
        "mapping_status": mapping.mapping_status,
        "mapping_confidence": _decimal_json(mapping.confidence),
        "price_status": price_context.status,
        "executable_price_source": price_context.source,
        "price_staleness_seconds": price_context.staleness_seconds,
    }


def _module_scores_from_summary(features: dict[str, object]) -> dict[str, Decimal]:
    summary = features.get("data_quality_summary")
    module_scores = summary.get("module_scores") if isinstance(summary, dict) else {}
    scores: dict[str, Decimal] = {}
    if isinstance(module_scores, dict):
        for module_name, value in module_scores.items():
            scores[str(module_name)] = _quality_decimal(value)
    return scores


def _quality_contribution_breakdown(
    *,
    scores: dict[str, Decimal],
    weights: dict[str, Decimal],
    features: dict[str, object],
) -> dict[str, object]:
    total_weight = sum(weights.values())
    all_modules = tuple(dict.fromkeys((*CORE_MODULES, *weights.keys())))
    contribution_by_module: dict[str, float] = {}
    penalty_by_module: dict[str, float] = {}
    score_by_module: dict[str, float] = {}
    status_by_module: dict[str, str] = {}
    role_by_module: dict[str, str] = {}
    weight_by_module: dict[str, float] = {}

    for module_name in all_modules:
        score = min(max(scores.get(module_name, Decimal("0")), Decimal("0")), Decimal("1"))
        weight = weights.get(module_name, Decimal("0"))
        normalized_weight = (weight / total_weight) if total_weight else Decimal("0")
        contribution = (score * normalized_weight).quantize(Decimal("0.0001"))
        penalty = max(normalized_weight - contribution, Decimal("0")).quantize(Decimal("0.0001"))
        contribution_by_module[module_name] = float(contribution)
        penalty_by_module[module_name] = float(penalty)
        score_by_module[module_name] = float(score.quantize(Decimal("0.0001")))
        status_by_module[module_name] = _module_status_from_features(features, module_name)
        role_by_module[module_name] = QUALITY_MODULE_ROLES.get(module_name, "supporting")
        weight_by_module[module_name] = float(normalized_weight.quantize(Decimal("0.0001")))

    return {
        "module_scores": score_by_module,
        "module_status": status_by_module,
        "module_role": role_by_module,
        "quality_contribution_by_module": contribution_by_module,
        "quality_penalty_by_module": penalty_by_module,
        "quality_weight_by_module": weight_by_module,
    }


def _paper_observation_quality_context(
    *,
    features: dict[str, object],
    market_type: str,
    candidate_stage_market_context: dict[str, object],
    model_score_data_quality: Decimal | None,
) -> QualityContext:
    raw_quality = _quality_decimal(features.get("data_quality"), default=Decimal("0")).quantize(Decimal("0.0001"))
    raw_scores = _module_scores_from_summary(features)
    paper_scores = dict(raw_scores)
    paper_scores["market_context"] = _status_score(
        str(candidate_stage_market_context.get("source_status") or "missing"),
        _quality_decimal(candidate_stage_market_context.get("completeness"), Decimal("0")),
    )
    total_weight = sum(PAPER_OBSERVATION_QUALITY_WEIGHTS.values())
    weighted = sum(
        paper_scores.get(module_name, Decimal("0")) * weight
        for module_name, weight in PAPER_OBSERVATION_QUALITY_WEIGHTS.items()
    )
    profile_quality = (weighted / total_weight if total_weight else Decimal("0")).quantize(Decimal("0.0001"))
    paper_quality = profile_quality
    quality_block_reason = list(
        str(reason)
        for reason in ((features.get("data_quality_summary") or {}).get("data_quality_reason") or [])
        if reason
    )

    if paper_scores.get("starter_identity", Decimal("0")) == Decimal("0"):
        paper_quality = min(paper_quality, Decimal("0.6000"))
        quality_block_reason.append("PAPER_OBSERVATION_CAP_STARTER_IDENTITY_MISSING")
    if (
        paper_scores.get("offense_season", Decimal("0")) == Decimal("0")
        and paper_scores.get("offense_recent", Decimal("0")) == Decimal("0")
    ):
        paper_quality = min(paper_quality, Decimal("0.6500"))
        quality_block_reason.append("PAPER_OBSERVATION_CAP_OFFENSE_MISSING")

    threshold = _paper_quality_threshold()
    raw_weights = QUALITY_WEIGHTS.get(market_type or FULL_GAME_WINNER, QUALITY_WEIGHTS[FULL_GAME_WINNER])
    raw_breakdown = _quality_contribution_breakdown(scores=raw_scores, weights=raw_weights, features=features)
    paper_breakdown = _quality_contribution_breakdown(
        scores=paper_scores,
        weights=PAPER_OBSERVATION_QUALITY_WEIGHTS,
        features=features,
    )
    paper_breakdown["module_status"]["market_context"] = str(
        candidate_stage_market_context.get("source_status") or "missing"
    )
    paper_breakdown["module_role"]["market_context"] = "candidate_stage"

    top_penalties = sorted(
        (
            {
                "module": module_name,
                "penalty": penalty,
                "status": paper_breakdown["module_status"].get(module_name),
                "role": paper_breakdown["module_role"].get(module_name),
            }
            for module_name, penalty in paper_breakdown["quality_penalty_by_module"].items()
            if penalty
        ),
        key=lambda item: float(item["penalty"]),
        reverse=True,
    )[:8]
    if paper_quality < threshold:
        quality_block_reason.append("PAPER_OBSERVATION_QUALITY_BELOW_THRESHOLD")
        quality_block_reason.extend(
            f"{item['module'].upper()}_{str(item['status']).upper()}" for item in top_penalties[:3]
        )

    decomposition = {
        "raw_feature_snapshot_data_quality": float(raw_quality),
        "model_score_data_quality": _decimal_json(model_score_data_quality),
        "paper_observation_profile_data_quality": float(profile_quality),
        "paper_observation_data_quality": float(paper_quality),
        "quality_threshold": float(threshold),
        "candidate_stage_market_context": candidate_stage_market_context,
        "raw_feature_snapshot": raw_breakdown,
        "paper_observation": paper_breakdown,
        "quality_block_reason": list(dict.fromkeys(quality_block_reason)),
        "top_quality_penalties": top_penalties,
    }
    return QualityContext(
        raw_feature_snapshot_data_quality=raw_quality,
        paper_observation_data_quality=paper_quality,
        threshold=threshold,
        candidate_stage_market_context=candidate_stage_market_context,
        decomposition=decomposition,
        quality_block_reason=list(dict.fromkeys(quality_block_reason)),
    )


def _ev_decomposition(
    *,
    probability: Decimal | None,
    price: Decimal | None,
    gross_ev: Decimal | None,
    fee_estimate: Decimal | None,
    net_ev: Decimal | None,
    probability_edge: Decimal | None,
) -> dict[str, object]:
    settings = get_settings()
    return {
        "probability": _decimal_json(probability),
        "executable_price": _decimal_json(price),
        "gross_expected_value": _decimal_json(gross_ev),
        "fee_estimate": _decimal_json(fee_estimate),
        "net_expected_value": _decimal_json(net_ev),
        "probability_edge": _decimal_json(probability_edge),
        "paper_min_net_ev": float(settings.paper_min_net_ev),
        "paper_min_prob_edge": float(settings.paper_min_prob_edge),
        "gross_ev_pass": gross_ev is not None and gross_ev > Decimal("0"),
        "net_ev_pass": net_ev is not None and net_ev >= settings.paper_min_net_ev,
        "probability_edge_pass": probability_edge is not None and probability_edge >= settings.paper_min_prob_edge,
        "fee_present": fee_estimate is not None,
    }


def _scope_for_family(family: str | None) -> str:
    return "first_five" if (family or "").startswith("first_five") else "full_game"


def _paper_quality_threshold() -> Decimal:
    return get_settings().paper_observation_min_data_quality


def _sizing_context(
    *,
    bankroll: Decimal,
    price: Decimal | None,
    one_contract_net_ev: Decimal | None,
) -> SizingContext:
    settings = get_settings()
    if settings.paper_position_sizing_mode.strip().lower() != "fixed_risk":
        quantity = max(settings.default_paper_contracts, 1)
        fee = _estimate_trade_fee(price, quantity)
        one_fee = _estimate_trade_fee(price, 1)
        cost_per_contract = (price + (one_fee or Decimal("0"))).quantize(Decimal("0.000001")) if price else None
        total_cost = (cost_per_contract * Decimal(quantity)).quantize(Decimal("0.01")) if cost_per_contract else None
        sized_ev = (one_contract_net_ev * Decimal(quantity)).quantize(Decimal("0.000001")) if one_contract_net_ev else None
        return SizingContext(
            bankroll_at_entry=bankroll.quantize(Decimal("0.01")),
            risk_pct=None,
            risk_dollars=None,
            contracts=quantity,
            estimated_cost_per_contract=cost_per_contract,
            estimated_total_cost=total_cost,
            one_contract_expected_value=one_contract_net_ev,
            sized_expected_value=sized_ev,
            one_contract_fee_estimate=one_fee,
            total_fee_estimate=fee,
            status="default_contracts",
        )

    if not _is_executable_price(price):
        return SizingContext(None, None, None, 0, None, None, one_contract_net_ev, None, None, None, "missing_price")

    one_fee = _estimate_trade_fee(price, 1)
    if one_fee is None:
        return SizingContext(None, None, None, 0, None, None, one_contract_net_ev, None, None, None, "missing_fee")

    risk_pct = settings.paper_risk_per_trade_pct
    risk_dollars = (bankroll * risk_pct).quantize(Decimal("0.01"))
    cost_per_contract = (price + one_fee).quantize(Decimal("0.000001"))
    raw_contracts = int((risk_dollars / cost_per_contract).to_integral_value(rounding=ROUND_FLOOR))
    if raw_contracts < settings.paper_min_contracts:
        return SizingContext(
            bankroll_at_entry=bankroll.quantize(Decimal("0.01")),
            risk_pct=risk_pct,
            risk_dollars=risk_dollars,
            contracts=0,
            estimated_cost_per_contract=cost_per_contract,
            estimated_total_cost=None,
            one_contract_expected_value=one_contract_net_ev,
            sized_expected_value=None,
            one_contract_fee_estimate=one_fee,
            total_fee_estimate=None,
            status="insufficient_bankroll_or_contract_size",
        )

    contracts = min(raw_contracts, settings.paper_max_contracts_per_trade)
    total_fee = _estimate_trade_fee(price, contracts)
    total_cost = ((price * Decimal(contracts)) + (total_fee or Decimal("0"))).quantize(Decimal("0.01"))
    sized_ev = (one_contract_net_ev * Decimal(contracts)).quantize(Decimal("0.000001")) if one_contract_net_ev else None
    if total_fee is not None and one_fee is not None:
        sized_ev = (
            ((one_contract_net_ev or Decimal("0")) + one_fee) * Decimal(contracts) - total_fee
        ).quantize(Decimal("0.000001"))
    return SizingContext(
        bankroll_at_entry=bankroll.quantize(Decimal("0.01")),
        risk_pct=risk_pct,
        risk_dollars=risk_dollars,
        contracts=contracts,
        estimated_cost_per_contract=cost_per_contract,
        estimated_total_cost=total_cost,
        one_contract_expected_value=one_contract_net_ev,
        sized_expected_value=sized_ev,
        one_contract_fee_estimate=one_fee,
        total_fee_estimate=total_fee,
        status="sized",
    )


def _diagnostics_payload(
    *,
    mapping: MarketMapping,
    game: MlbGame,
    market: KalshiMarket,
    market_type: str,
    target_date: date,
    minutes_to_start: int,
    price_context: PriceContext,
    probability: Decimal | None,
    gross_ev: Decimal | None,
    fee_estimate: Decimal | None,
    net_ev: Decimal | None,
    probability_edge: Decimal | None,
    data_quality: Decimal | None,
    calibration_status: str | None,
    push_probability: Decimal | None,
    open_trade_exists: bool,
) -> dict[str, object]:
    settings = get_settings()
    settlement_status = _settlement_status(mapping, market)
    mapping_ok = mapping.mapping_status != "needs_review" and (mapping.confidence or Decimal("0")) >= Decimal("0.55")
    supported_ok = market_type in PAPER_SUPPORTED_MARKET_FAMILIES
    if market_type != FULL_GAME_WINNER and settlement_status != "paper_supported":
        supported_ok = False
    game_not_started = minutes_to_start > 0 and _eastern_date(game.scheduled_start) == target_date
    market_open = market.status.strip().lower() in TRADABLE_MARKET_STATUSES
    price_ok = price_context.status == "fresh_executable"
    spread_verification = _spread_verification_for_candidate_gate(
        game=game,
        mapping=mapping,
        market=market,
        market_type=market_type,
    )
    full_game_spread_audit_reason = (
        _full_game_spread_audit_rejection_reason(spread_verification)
        if market_type == FULL_GAME_SPREAD
        else None
    )
    full_game_spread_audit_trusted = market_type != FULL_GAME_SPREAD or full_game_spread_audit_reason is None
    spread_trading_ok = (
        settings.paper_full_game_spread_trading_enabled and full_game_spread_audit_trusted
        if market_type == FULL_GAME_SPREAD
        else settings.paper_spread_trading_enabled or market_type != FIRST_FIVE_SPREAD
    )
    spread_parser_ok = _spread_parser_verified(mapping, game, market, market_type)
    data_quality_ok = data_quality is not None and data_quality >= _paper_quality_threshold()
    push_ok = not (push_probability is not None and push_probability > Decimal("0") and market_type.endswith(("spread", "total")))
    probability_present = probability is not None
    gross_ev_positive = gross_ev is not None and gross_ev > Decimal("0")
    fee_present = fee_estimate is not None
    probability_edge_ok = probability_edge is not None and probability_edge >= settings.paper_min_prob_edge
    net_ev_ok = net_ev is not None and net_ev >= settings.paper_min_net_ev
    calibration_ok = not settings.paper_require_calibrated_for_trade or calibration_status == "calibrated"
    open_position_ok = not open_trade_exists
    selection_code = (mapping.selection_code or market.selection_code or "").upper()
    f5_tie_enabled = not (market_type == FIRST_FIVE_WINNER and selection_code == "TIE")
    selection_trusted_ok = (
        not settings.safe_execution_posture
        or market_type not in SELECTION_REQUIRED_FAMILIES
        or _has_trusted_candidate_selection(mapping, game, market)
    )
    executable_price = price_context.executable_price
    price_floor_ok = executable_price is None or executable_price >= settings.paper_min_trade_price
    low_price_candidate = (
        executable_price is not None
        and executable_price >= settings.paper_min_trade_price
        and executable_price < settings.paper_low_price_threshold
    )
    low_price_probability_edge_ok = (
        not low_price_candidate
        or (probability_edge is not None and probability_edge >= settings.paper_low_price_min_prob_edge)
    )
    low_price_net_ev_ok = (
        not low_price_candidate
        or (net_ev is not None and net_ev >= settings.paper_low_price_min_net_ev)
    )
    pre_quality_gates = [
        mapping_ok,
        supported_ok,
        game_not_started,
        market_open,
        f5_tie_enabled,
        selection_trusted_ok,
        spread_trading_ok,
        spread_parser_ok,
        price_ok,
        price_floor_ok,
        low_price_probability_edge_ok,
        low_price_net_ev_ok,
        push_ok,
        probability_present,
        gross_ev_positive,
        fee_present,
        probability_edge_ok,
        net_ev_ok,
        calibration_ok,
        settings.paper_candidate_engine_enabled,
        open_position_ok,
    ]
    before_quality = all(pre_quality_gates)
    after_quality = before_quality and data_quality_ok
    flags = {
        "gate_mapping_ok": mapping_ok and supported_ok,
        "gate_market_open": market_open,
        "gate_game_not_started": game_not_started,
        "gate_f5_tie_enabled": f5_tie_enabled,
        "gate_selection_trusted_ok": selection_trusted_ok,
        "gate_spread_trading_enabled": spread_trading_ok,
        "paper_spread_trading_enabled": settings.paper_spread_trading_enabled,
        "paper_first_five_spread_trading_enabled": settings.paper_spread_trading_enabled,
        "paper_full_game_spread_trading_enabled": settings.paper_full_game_spread_trading_enabled,
        "full_game_spread_audit_gate_enabled": True,
        "full_game_spread_requires_trusted_audit": True,
        "full_game_spread_audit_only": market_type == FULL_GAME_SPREAD
        and not settings.paper_full_game_spread_trading_enabled,
        "gate_full_game_spread_audit_trusted": full_game_spread_audit_trusted,
        "full_game_spread_audit_status": spread_verification.audit_status if spread_verification else None,
        "full_game_spread_audit_rejection_reason": full_game_spread_audit_reason,
        "gate_spread_parser_verified": spread_parser_ok,
        "gate_price_fresh_executable": price_ok,
        "gate_price_floor_ok": price_floor_ok,
        "gate_low_price_probability_edge_ok": low_price_probability_edge_ok,
        "gate_low_price_net_ev_ok": low_price_net_ev_ok,
        "gate_data_quality_ok": data_quality_ok,
        "gate_push_ok": push_ok,
        "gate_probability_present": probability_present,
        "gate_gross_ev_positive": gross_ev_positive,
        "gate_fee_present": fee_present,
        "gate_probability_edge_ok": probability_edge_ok,
        "gate_net_ev_ok": net_ev_ok,
        "gate_calibration_ok": calibration_ok,
        "gate_line_selection_ok": True,
        "gate_game_scope_correlation_ok": True,
        "gate_caps_ok": True,
        "gate_open_position_ok": open_position_ok,
        "gate_final_trade_eligible": after_quality,
        "blocked_by_quality_only": before_quality and not data_quality_ok,
        "would_pass_ev_if_quality_allowed": gross_ev_positive and net_ev_ok,
        "would_pass_edge_if_quality_allowed": probability_edge_ok,
        "ev_edge_pass_but_quality_fail": gross_ev_positive and net_ev_ok and probability_edge_ok and not data_quality_ok,
        "counterfactual_trade_eligible_before_quality": before_quality,
        "counterfactual_trade_eligible_after_quality": after_quality,
        "market_type_supported": supported_ok,
        "contract_side": price_context.side,
        "paper_quality_threshold": float(_paper_quality_threshold()),
        "low_price_candidate": low_price_candidate,
        "paper_min_trade_price": float(settings.paper_min_trade_price),
        "paper_low_price_threshold": float(settings.paper_low_price_threshold),
        "paper_low_price_min_net_ev": float(settings.paper_low_price_min_net_ev),
        "paper_low_price_min_prob_edge": float(settings.paper_low_price_min_prob_edge),
    }
    return flags


def _apply_gate_fields(candidate: ModelCandidate, diagnostics: dict[str, object]) -> None:
    candidate.gate_diagnostics = diagnostics
    for key, value in diagnostics.items():
        if key.startswith("gate_") or key in {
            "blocked_by_quality_only",
            "would_pass_ev_if_quality_allowed",
            "would_pass_edge_if_quality_allowed",
            "ev_edge_pass_but_quality_fail",
            "counterfactual_trade_eligible_before_quality",
            "counterfactual_trade_eligible_after_quality",
        }:
            if hasattr(candidate, key):
                setattr(candidate, key, bool(value))


def _update_gate(candidate: ModelCandidate, key: str, value: bool) -> None:
    diagnostics = dict(candidate.gate_diagnostics or {})
    diagnostics[key] = value
    final = all(
        bool(diagnostics.get(flag))
        for flag in (
            "gate_mapping_ok",
            "gate_market_open",
            "gate_game_not_started",
            "gate_f5_tie_enabled",
            "gate_selection_trusted_ok",
            "gate_spread_trading_enabled",
            "gate_spread_parser_verified",
            "gate_price_fresh_executable",
            "gate_price_floor_ok",
            "gate_low_price_probability_edge_ok",
            "gate_low_price_net_ev_ok",
            "gate_data_quality_ok",
            "gate_push_ok",
            "gate_probability_present",
            "gate_gross_ev_positive",
            "gate_fee_present",
            "gate_probability_edge_ok",
            "gate_net_ev_ok",
            "gate_calibration_ok",
            "gate_line_selection_ok",
            "gate_game_scope_correlation_ok",
            "gate_caps_ok",
            "gate_open_position_ok",
        )
    )
    diagnostics["gate_final_trade_eligible"] = final
    _apply_gate_fields(candidate, diagnostics)


def _market_classification_text(market: KalshiMarket) -> str:
    return " ".join(
        value or ""
        for value in (
            market.title,
            market.subtitle,
            market.rules,
            market.yes_subtitle,
            market.no_subtitle,
            market.ticker,
            market.event_ticker,
        )
    )


def _update_candidate_diagnostics(candidate: ModelCandidate, values: dict[str, object]) -> None:
    diagnostics = dict(candidate.gate_diagnostics or {})
    diagnostics.update(values)
    candidate.gate_diagnostics = diagnostics


def _candidate_ids_with_trades(session: Session, candidate_ids: list[int], epoch_id: int | None) -> set[int]:
    if not candidate_ids:
        return set()
    query = select(PaperTrade.candidate_id).where(PaperTrade.candidate_id.in_(candidate_ids))
    if epoch_id is not None:
        query = query.where(PaperTrade.paper_trading_epoch_id == epoch_id)
    return {
        candidate_id
        for candidate_id in session.scalars(query)
        if candidate_id is not None
    }


def _open_trade_for_market(
    session: Session,
    market_ticker: str,
    contract_side: str,
    epoch_id: int | None,
) -> PaperTrade | None:
    query = (
        select(PaperTrade)
        .where(PaperTrade.market_ticker == market_ticker)
        .where(PaperTrade.contract_side == contract_side)
        .where(PaperTrade.status == "open")
    )
    if epoch_id is not None:
        query = query.where(PaperTrade.paper_trading_epoch_id == epoch_id)
    return session.scalar(query.order_by(PaperTrade.entry_time.desc(), PaperTrade.id.desc()).limit(1))


def _open_trade_for_ticker(session: Session, market_ticker: str, epoch_id: int | None) -> PaperTrade | None:
    query = (
        select(PaperTrade)
        .where(PaperTrade.market_ticker == market_ticker)
        .where(PaperTrade.status == "open")
    )
    if epoch_id is not None:
        query = query.where(PaperTrade.paper_trading_epoch_id == epoch_id)
    return session.scalar(query.order_by(PaperTrade.entry_time.desc(), PaperTrade.id.desc()).limit(1))


def _has_trusted_candidate_selection(mapping: MarketMapping, game: MlbGame, market: KalshiMarket) -> bool:
    if has_trusted_selection(game, market.ticker):
        return True
    selection = (mapping.selection_code or market.selection_code or "").upper()
    return selection in game_team_codes(game)


def _settlement_status(mapping: MarketMapping, market: KalshiMarket) -> str | None:
    return mapping.settlement_rule_status or market.settlement_rule_status


def _compact_spread_audit_metadata(verification: SpreadVerification | None) -> dict[str, object] | None:
    if verification is None:
        return None
    metadata = verification.as_metadata()
    return {key: metadata.get(key) for key in COMPACT_SPREAD_AUDIT_KEYS if key in metadata}


def _trusted_full_game_spread_audit(verification: SpreadVerification | None) -> bool:
    if verification is None:
        return False
    reason_codes = set(verification.reason_codes or [])
    selected_team_ok = verification.selection_code is not None and "selected_team_verified" in reason_codes
    line_direction_ok = (
        verification.line_value is not None
        and verification.line_direction is not None
        and "selected_team_threshold_verified" in reason_codes
    )
    complement_ok = (
        verification.no_is_true_complement
        and verification.complement_safe_for_paper_settlement
        and "binary_yes_no_complement_verified" in reason_codes
    )
    push_ok = (not verification.push_possible) or verification.push_rule_verified
    settlement_ok = bool(verification.settlement_formula) and "settlement_formula_verified" in reason_codes
    return (
        verification.family_key == FULL_GAME_SPREAD
        and verification.inning_scope == "full_game"
        and verification.audit_status == FULL_GAME_SPREAD_TRUSTED_AUDIT_STATUS
        and verification.verified
        and selected_team_ok
        and line_direction_ok
        and complement_ok
        and push_ok
        and settlement_ok
        and verification.threshold_runs is not None
    )


def _full_game_spread_audit_rejection_reason(verification: SpreadVerification | None) -> str | None:
    if _trusted_full_game_spread_audit(verification):
        return None
    if verification is None:
        return "no_trade_full_game_spread_audit_missing"
    status = verification.audit_status or "needs_review"
    return FULL_GAME_SPREAD_AUDIT_REASON_BY_STATUS.get(status, "no_trade_full_game_spread_audit_not_trusted")


def _spread_verification_for_candidate_gate(
    *,
    game: MlbGame,
    mapping: MarketMapping,
    market: KalshiMarket,
    market_type: str,
) -> SpreadVerification | None:
    if market_type == FULL_GAME_SPREAD:
        return spread_verification_from_cached_metadata(mapping=mapping, market=market)
    if market_type in SPREAD_FAMILIES:
        return spread_verification_from_mapping(game=game, mapping=mapping, market=market)
    return None


def _spread_parser_verified(mapping: MarketMapping, game: MlbGame, market: KalshiMarket, market_type: str) -> bool:
    if market_type not in SPREAD_FAMILIES:
        return True
    return spread_verification_from_mapping(game=game, mapping=mapping, market=market).verified


def _base_decision(
    mapping: MarketMapping,
    game: MlbGame,
    market: KalshiMarket,
    market_type: str,
    target_date: date,
    minutes_to_start: int,
    price_context: PriceContext,
    probability: Decimal | None,
    gross_ev: Decimal | None,
    fee_estimate: Decimal | None,
    net_ev: Decimal | None,
    probability_edge: Decimal | None,
    data_quality: Decimal | None,
    calibration_status: str | None,
    push_probability: Decimal | None,
) -> str:
    settings = get_settings()
    if mapping.mapping_status == "needs_review" or (mapping.confidence or Decimal("0")) < Decimal("0.55"):
        return "no_trade_mapping_uncertain"
    if market_type not in PAPER_SUPPORTED_MARKET_FAMILIES:
        return "no_trade_unsupported_family"
    settlement_status = _settlement_status(mapping, market)
    if market_type != FULL_GAME_WINNER and settlement_status != "paper_supported":
        if mapping.line_value is None and market.line_value is None and market_type.endswith(("spread", "total")):
            return "no_trade_missing_line"
        return "no_trade_parse_uncertain"
    if market_type == FULL_GAME_SPREAD:
        if not settings.paper_full_game_spread_trading_enabled:
            return "no_trade_full_game_spread_trading_disabled"
        audit_reason = _full_game_spread_audit_rejection_reason(
            spread_verification_from_cached_metadata(mapping=mapping, market=market)
        )
        if audit_reason is not None:
            return audit_reason
    if market_type == FIRST_FIVE_SPREAD and not settings.paper_spread_trading_enabled:
        return "no_trade_spread_trading_disabled"
    if market_type in SPREAD_FAMILIES and not _spread_parser_verified(mapping, game, market, market_type):
        return "no_trade_spread_parser_unverified"
    if minutes_to_start <= 0:
        return "no_trade_game_started"
    if _eastern_date(game.scheduled_start) != target_date:
        return "no_trade_wrong_target_date"
    if market.status.strip().lower() not in TRADABLE_MARKET_STATUSES:
        return "no_trade_market_closed"
    selection_code = (mapping.selection_code or market.selection_code or "").upper()
    if market_type == FIRST_FIVE_WINNER and selection_code == "TIE":
        return "no_trade_f5_tie_disabled"
    if (
        settings.safe_execution_posture
        and market_type in SELECTION_REQUIRED_FAMILIES
        and not _has_trusted_candidate_selection(mapping, game, market)
    ):
        return "no_trade_untrusted_selection"
    if price_context.status == "missing":
        return "no_trade_missing_price"
    if price_context.status == "stale":
        return "no_trade_stale_price"
    if price_context.status != "fresh_executable":
        return "no_trade_non_executable_price"
    if price_context.executable_price is not None and price_context.executable_price < settings.paper_min_trade_price:
        return "no_trade_price_below_floor"
    if data_quality is None or data_quality < settings.paper_observation_min_data_quality:
        return "no_trade_low_data_quality"
    if push_probability is not None and push_probability > Decimal("0") and market_type.endswith(("spread", "total")):
        return "no_trade_push_possible"
    if probability is None:
        return "no_trade_missing_probability"
    if gross_ev is None or gross_ev <= Decimal("0"):
        return "no_trade_edge_too_low"
    if fee_estimate is None:
        return "no_trade_missing_fee_estimate"
    if probability_edge is None or probability_edge < settings.paper_min_prob_edge:
        return "no_trade_probability_edge_low"
    if net_ev is None or net_ev < settings.paper_min_net_ev:
        return "no_trade_fee_adjusted_ev_too_low"
    if (
        price_context.executable_price is not None
        and price_context.executable_price < settings.paper_low_price_threshold
    ):
        if probability_edge < settings.paper_low_price_min_prob_edge:
            return "no_trade_low_price_probability_edge_low"
        if net_ev < settings.paper_low_price_min_net_ev:
            return "no_trade_low_price_ev_too_low"
    if settings.paper_require_calibrated_for_trade and calibration_status != "calibrated":
        return "no_trade_uncalibrated_probability"
    if not settings.paper_candidate_engine_enabled:
        return "candidate_only"
    if settings.safe_execution_posture:
        return "eligible_for_paper_trade"
    return "candidate_only"


LINE_MARKET_FAMILIES = {
    FULL_GAME_SPREAD,
    FULL_GAME_TOTAL,
    FIRST_FIVE_SPREAD,
    FIRST_FIVE_TOTAL,
}


def _slate_trade_counts(
    session: Session,
    target_date: date,
    start: datetime,
    end: datetime,
    epoch_id: int | None,
) -> tuple[
    int,
    dict[int, int],
    dict[str, int],
    dict[tuple[int, str], int],
    set[str],
    int,
    dict[str, int],
]:
    settings = get_settings()
    query = (
        select(PaperTrade, ModelCandidate, MlbGame)
        .outerjoin(ModelCandidate, PaperTrade.candidate_id == ModelCandidate.id)
        .outerjoin(MlbGame, ModelCandidate.mlb_game_id == MlbGame.id)
        .where(
            (ModelCandidate.target_date == target_date)
            | (
                (ModelCandidate.target_date.is_(None))
                & (MlbGame.scheduled_start >= start)
                & (MlbGame.scheduled_start < end)
            )
            | (
                (PaperTrade.entry_time >= start)
                & (PaperTrade.entry_time < end)
                & (
                    (ModelCandidate.id.is_(None))
                    | ((ModelCandidate.target_date.is_(None)) & (MlbGame.id.is_(None)))
                )
            )
        )
    )
    if epoch_id is not None:
        query = query.where(PaperTrade.paper_trading_epoch_id == epoch_id)
    rows = list(
        session.execute(query)
    )
    game_counts: dict[int, int] = {}
    family_counts: dict[str, int] = {}
    game_family_counts: dict[tuple[int, str], int] = {}
    market_tickers: set[str] = set()
    low_price_count = 0
    side_counts: dict[str, int] = {}
    for trade, candidate, _game in rows:
        market_tickers.add(trade.market_ticker)
        if trade.entry_price < settings.paper_low_price_threshold:
            low_price_count += 1
        side_key = (trade.contract_side or "unknown").lower()
        side_counts[side_key] = side_counts.get(side_key, 0) + 1
        if candidate and candidate.mlb_game_id is not None:
            game_counts[candidate.mlb_game_id] = game_counts.get(candidate.mlb_game_id, 0) + 1
        family = trade.market_family or (candidate.market_family if candidate else None) or "unknown"
        family_counts[family] = family_counts.get(family, 0) + 1
        if candidate and candidate.mlb_game_id is not None:
            key = (candidate.mlb_game_id, family)
            game_family_counts[key] = game_family_counts.get(key, 0) + 1
    return len(rows), game_counts, family_counts, game_family_counts, market_tickers, low_price_count, side_counts


def _open_position_count(session: Session, epoch_id: int | None) -> int:
    query = select(func.count(PaperTrade.id)).where(PaperTrade.status == "open")
    if epoch_id is not None:
        query = query.where(PaperTrade.paper_trading_epoch_id == epoch_id)
    return int(session.scalar(query) or 0)


def _trade_rank_score(candidate: ModelCandidate) -> Decimal:
    ev = candidate.net_expected_value or Decimal("0")
    data_quality = candidate.data_quality or Decimal("0")
    probability = candidate.probability_calibrated or candidate.model_probability or Decimal("0")
    return (ev * Decimal("10")) + data_quality + probability


def _game_family_trade_limit(family: str) -> int:
    settings = get_settings()
    limit = max(settings.paper_max_trades_per_game_family, 1)
    if family == FIRST_FIVE_WINNER:
        return limit if settings.paper_allow_multiple_f5_winner_outcomes else 1
    if family in LINE_MARKET_FAMILIES:
        return limit if settings.paper_allow_multiple_lines_per_game_family else 1
    return 1


def _apply_line_selection(intents: list[TradeIntent]) -> tuple[list[TradeIntent], dict[str, int]]:
    counts = {
        "line_selection_groups_considered": 0,
        "line_selection_candidates_kept": 0,
        "line_selection_candidates_rejected": 0,
    }
    if not intents:
        return [], counts

    grouped: dict[tuple[date | None, int | None, str], list[TradeIntent]] = {}
    for intent in intents:
        family = intent.candidate.market_family or "unknown"
        key = (intent.candidate.target_date, intent.candidate.mlb_game_id, family)
        grouped.setdefault(key, []).append(intent)

    selected: list[TradeIntent] = []
    for group in grouped.values():
        counts["line_selection_groups_considered"] += 1
        ranked = sorted(group, key=lambda item: item.score, reverse=True)
        family = ranked[0].candidate.market_family or "unknown"
        limit = _game_family_trade_limit(family)
        kept = ranked[:limit]
        selected.extend(kept)
        counts["line_selection_candidates_kept"] += len(kept)
        for accepted in kept:
            _update_gate(accepted.candidate, "gate_line_selection_ok", True)
        for rejected in ranked[limit:]:
            rejected.candidate.decision = "no_trade_line_selection_not_best"
            _update_gate(rejected.candidate, "gate_line_selection_ok", False)
            counts["line_selection_candidates_rejected"] += 1

    return selected, counts


def _game_scope_key_from_candidate(candidate: ModelCandidate) -> tuple[date | None, int, str] | None:
    if candidate.mlb_game_id is None:
        return None
    family = candidate.market_family or candidate.market_type or "unknown"
    scope = candidate.inning_scope or _risk_scope(family)
    return candidate.target_date, candidate.mlb_game_id, scope


def _existing_open_game_scope_counts(session: Session, epoch_id: int | None) -> dict[tuple[date | None, int, str], int]:
    query = (
        select(PaperTrade, ModelCandidate)
        .outerjoin(ModelCandidate, PaperTrade.candidate_id == ModelCandidate.id)
        .where(PaperTrade.status == "open")
    )
    if epoch_id is not None:
        query = query.where(PaperTrade.paper_trading_epoch_id == epoch_id)
    counts: dict[tuple[date | None, int, str], int] = {}
    for _trade, candidate in session.execute(query):
        if candidate is None:
            continue
        key = _game_scope_key_from_candidate(candidate)
        if key is not None:
            counts[key] = counts.get(key, 0) + 1
    return counts


def _apply_game_scope_correlation(
    session: Session,
    intents: list[TradeIntent],
    epoch_id: int | None,
) -> tuple[list[TradeIntent], dict[str, int], dict[str, object]]:
    settings = get_settings()
    limit = max(settings.paper_max_trades_per_game_scope, 1)
    counts = {
        "game_scope_correlation_groups_considered": 0,
        "game_scope_correlation_candidates_kept": 0,
        "game_scope_correlation_candidates_rejected": 0,
        "no_trade_game_scope_correlation_cap": 0,
        "no_trade_same_game_scope_correlation_not_best": 0,
    }
    if not intents:
        return [], counts, {"limit": limit, "groups": {}}

    existing_counts = _existing_open_game_scope_counts(session, epoch_id)
    grouped: dict[tuple[date | None, int, str], list[TradeIntent]] = {}
    passthrough: list[TradeIntent] = []
    for intent in intents:
        key = _game_scope_key_from_candidate(intent.candidate)
        if key is None:
            passthrough.append(intent)
            continue
        grouped.setdefault(key, []).append(intent)

    selected = list(passthrough)
    groups_summary: dict[str, dict[str, object]] = {}
    for key, group in grouped.items():
        counts["game_scope_correlation_groups_considered"] += 1
        target_date, game_id, scope = key
        group_label = f"{target_date.isoformat() if target_date else 'none'}:{game_id}:{scope}"
        ranked = sorted(group, key=lambda item: item.score, reverse=True)
        existing = existing_counts.get(key, 0)
        available = max(limit - existing, 0)
        kept = ranked[:available]
        selected.extend(kept)
        counts["game_scope_correlation_candidates_kept"] += len(kept)
        groups_summary[group_label] = {
            "target_date": target_date.isoformat() if target_date else None,
            "mlb_game_id": game_id,
            "inning_scope": scope,
            "limit": limit,
            "existing_open": existing,
            "considered": len(ranked),
            "kept": len(kept),
        }
        for index, accepted in enumerate(kept, start=1):
            _update_candidate_diagnostics(
                accepted.candidate,
                {
                    "gate_game_scope_correlation_ok": True,
                    "correlation_group_key": group_label,
                    "correlation_rank": index,
                    "correlation_rank_score": float(accepted.score),
                    "correlation_limit": limit,
                    "correlation_existing_open": existing,
                },
            )
        for index, rejected in enumerate(ranked[available:], start=available + 1):
            rejected.candidate.decision = (
                "no_trade_game_scope_correlation_cap"
                if available <= 0
                else "no_trade_same_game_scope_correlation_not_best"
            )
            _update_gate(rejected.candidate, "gate_game_scope_correlation_ok", False)
            _update_candidate_diagnostics(
                rejected.candidate,
                {
                    "correlation_group_key": group_label,
                    "correlation_rank": index,
                    "correlation_rank_score": float(rejected.score),
                    "correlation_limit": limit,
                    "correlation_existing_open": existing,
                    "correlation_rejection_reason": rejected.candidate.decision,
                },
            )
            counts["game_scope_correlation_candidates_rejected"] += 1
            counts[rejected.candidate.decision] = counts.get(rejected.candidate.decision, 0) + 1
            session.add(rejected.candidate)

    return selected, counts, {"limit": limit, "groups": groups_summary}


def _apply_side_conflict_guard(intents: list[TradeIntent]) -> tuple[list[TradeIntent], dict[str, int]]:
    counts = {"no_trade_conflicting_side_signals": 0}
    grouped: dict[str, list[TradeIntent]] = {}
    for intent in intents:
        grouped.setdefault(intent.market.ticker, []).append(intent)

    selected: list[TradeIntent] = []
    for group in grouped.values():
        sides = {str(intent.candidate.contract_side or "").lower() for intent in group}
        if {"yes", "no"}.issubset(sides):
            for intent in group:
                intent.candidate.decision = "no_trade_conflicting_side_signals"
                _update_gate(intent.candidate, "gate_caps_ok", False)
                counts["no_trade_conflicting_side_signals"] += 1
            continue
        selected.extend(group)
    return selected, counts


def _apply_trade_caps(
    session: Session,
    intents: list[TradeIntent],
    target_date: date,
    day_start: datetime,
    day_end: datetime,
    epoch_id: int | None,
    *,
    enforce_slate_cap: bool = True,
    enforce_sweep_cap: bool = True,
    enforce_time_reserve: bool = True,
    enforce_open_position_cap: bool = True,
    enforce_low_price_new_caps: bool = True,
    enforce_side_new_caps: bool = True,
) -> tuple[list[TradeIntent], dict[str, int], dict[str, object]]:
    settings = get_settings()
    (
        existing_slate,
        game_counts,
        family_counts,
        game_family_counts,
        market_tickers,
        existing_low_price,
        side_counts,
    ) = _slate_trade_counts(
        session,
        target_date,
        day_start,
        day_end,
        epoch_id,
    )
    open_positions = _open_position_count(session, epoch_id)
    now_et = utc_now().astimezone(get_dashboard_zone())
    early_window = now_et.time() < time(15, 0)
    daily_cap = settings.paper_max_trades_per_slate
    reserved_later = settings.paper_reserve_trades_after_3pm_et if early_window else 0
    early_allowed = (
        min(settings.paper_max_new_trades_before_3pm_et, max(daily_cap - reserved_later, 0))
        if early_window
        else daily_cap
    )
    selected: list[TradeIntent] = []
    selected_low_price = 0
    selected_side_counts: dict[str, int] = {}
    cap_counts = {
        "candidate_only_due_to_trade_cap": 0,
        "no_trade_market_family_cap": 0,
        "no_trade_game_cap": 0,
        "no_trade_slate_cap": 0,
        "no_trade_sweep_cap_reached": 0,
        "no_trade_time_bucket_reserve": 0,
        "no_trade_correlated_market_cap": 0,
        "no_trade_game_family_cap": 0,
        "no_trade_open_position_cap": 0,
        "no_trade_low_price_slate_cap": 0,
        "no_trade_low_price_sweep_cap": 0,
        "no_trade_side_concentration_cap": 0,
    }

    for intent in sorted(intents, key=lambda item: item.score, reverse=True):
        candidate = intent.candidate
        family = candidate.market_family or "unknown"
        game_id = candidate.mlb_game_id
        side = (candidate.contract_side or "unknown").lower()
        low_price_trade = intent.price < settings.paper_low_price_threshold
        if intent.market.ticker in market_tickers:
            candidate.decision = "no_trade_correlated_market_cap"
            _update_gate(candidate, "gate_caps_ok", False)
        elif enforce_slate_cap and existing_slate + len(selected) >= settings.paper_max_trades_per_slate:
            candidate.decision = "no_trade_slate_cap"
            _update_gate(candidate, "gate_caps_ok", False)
        elif enforce_sweep_cap and len(selected) >= settings.paper_max_new_trades_per_sweep:
            candidate.decision = "no_trade_sweep_cap_reached"
            _update_gate(candidate, "gate_caps_ok", False)
        elif enforce_time_reserve and early_window and existing_slate + len(selected) >= early_allowed:
            candidate.decision = "no_trade_time_bucket_reserve"
            _update_gate(candidate, "gate_caps_ok", False)
        elif enforce_open_position_cap and open_positions + len(selected) >= settings.paper_max_open_positions:
            candidate.decision = "no_trade_open_position_cap"
            _update_gate(candidate, "gate_open_position_ok", False)
        elif game_id is not None and game_counts.get(game_id, 0) >= settings.paper_max_trades_per_game:
            candidate.decision = "no_trade_game_cap"
            _update_gate(candidate, "gate_caps_ok", False)
        elif family_counts.get(family, 0) >= settings.paper_max_trades_per_market_family:
            candidate.decision = "no_trade_market_family_cap"
            _update_gate(candidate, "gate_caps_ok", False)
        elif (
            game_id is not None
            and game_family_counts.get((game_id, family), 0) >= _game_family_trade_limit(family)
        ):
            candidate.decision = "no_trade_game_family_cap"
            _update_gate(candidate, "gate_caps_ok", False)
        elif (
            enforce_low_price_new_caps
            and low_price_trade
            and existing_low_price + selected_low_price >= settings.paper_low_price_max_trades_per_slate
        ):
            candidate.decision = "no_trade_low_price_slate_cap"
            _update_gate(candidate, "gate_caps_ok", False)
        elif (
            enforce_low_price_new_caps
            and low_price_trade
            and selected_low_price >= settings.paper_low_price_max_trades_per_sweep
        ):
            candidate.decision = "no_trade_low_price_sweep_cap"
            _update_gate(candidate, "gate_caps_ok", False)
        elif (
            enforce_side_new_caps
            and side in {"yes", "no"}
            and side_counts.get(side, 0) + selected_side_counts.get(side, 0)
            >= settings.paper_max_same_side_trades_per_slate
        ):
            candidate.decision = "no_trade_side_concentration_cap"
            _update_gate(candidate, "gate_caps_ok", False)
        else:
            candidate.decision = "paper_trade"
            _update_gate(candidate, "gate_caps_ok", True)
            _update_gate(candidate, "gate_open_position_ok", True)
            selected.append(intent)
            family_counts[family] = family_counts.get(family, 0) + 1
            if low_price_trade:
                selected_low_price += 1
            if side in {"yes", "no"}:
                selected_side_counts[side] = selected_side_counts.get(side, 0) + 1
            if game_id is not None:
                game_counts[game_id] = game_counts.get(game_id, 0) + 1
                game_family_counts[(game_id, family)] = game_family_counts.get((game_id, family), 0) + 1
            market_tickers.add(intent.market.ticker)
            continue
        cap_counts[candidate.decision] = cap_counts.get(candidate.decision, 0) + 1
        _update_candidate_diagnostics(
            candidate,
            {
                "cap_rejection_reason": candidate.decision,
                "existing_slate_trades": existing_slate,
                "existing_open_trades": open_positions,
                "new_trades_this_sweep_before_candidate": len(selected),
                "daily_slate_cap": daily_cap,
                "per_sweep_cap": settings.paper_max_new_trades_per_sweep,
                "early_window": early_window,
                "early_window_allowed": early_allowed,
                "reserved_later_slot_count": reserved_later,
                "low_price_trade": low_price_trade,
                "low_price_existing_slate": existing_low_price,
                "low_price_new_this_sweep": selected_low_price,
                "side_existing_slate": side_counts,
                "side_new_this_sweep": selected_side_counts,
                "same_side_slate_cap": settings.paper_max_same_side_trades_per_slate,
            },
        )
        session.add(candidate)

    summary = {
        "existing_slate_trades": existing_slate,
        "existing_open_trades": open_positions,
        "new_trades_this_sweep": len(selected),
        "daily_slate_cap": daily_cap,
        "per_sweep_cap": settings.paper_max_new_trades_per_sweep,
        "early_window": early_window,
        "early_window_used": existing_slate + len(selected) if early_window else None,
        "early_window_allowed": early_allowed if early_window else None,
        "reserved_later_slot_count": reserved_later,
        "low_price_existing_slate": existing_low_price,
        "low_price_new_this_sweep": selected_low_price,
        "low_price_slate_cap": settings.paper_low_price_max_trades_per_slate,
        "low_price_sweep_cap": settings.paper_low_price_max_trades_per_sweep,
        "low_price_threshold": float(settings.paper_low_price_threshold),
        "side_existing_slate": side_counts,
        "side_new_this_sweep": selected_side_counts,
        "same_side_slate_cap": settings.paper_max_same_side_trades_per_slate,
    }
    return selected, cap_counts, summary


def _trade_risk_cost(trade: PaperTrade) -> Decimal:
    if trade.estimated_total_cost is not None:
        return Decimal(trade.estimated_total_cost).quantize(Decimal("0.01"))
    return ((trade.entry_price * Decimal(trade.quantity)) + paper_trade_fee(trade)).quantize(Decimal("0.01"))


def _risk_scope(value: str | None) -> str:
    return "first_five" if (value or "").startswith("first_five") else "full_game"


def _risk_family(candidate: ModelCandidate | None, trade: PaperTrade | None = None) -> str:
    return (trade.market_family if trade else None) or (candidate.market_family if candidate else None) or "unknown"


def _daily_and_open_risk_usage(
    session: Session,
    target_date: date,
    day_start: datetime,
    day_end: datetime,
    epoch_id: int | None,
) -> dict[str, object]:
    settings = get_settings()
    query = select(PaperTrade, ModelCandidate).outerjoin(ModelCandidate, PaperTrade.candidate_id == ModelCandidate.id)
    if epoch_id is not None:
        query = query.where(PaperTrade.paper_trading_epoch_id == epoch_id)
    daily = Decimal("0.00")
    open_risk = Decimal("0.00")
    family: dict[str, Decimal] = {}
    scope: dict[str, Decimal] = {}
    low_price = Decimal("0.00")
    for trade, candidate in session.execute(query):
        cost = _trade_risk_cost(trade)
        is_target_day_trade = (
            candidate is not None
            and candidate.target_date == target_date
        ) or (day_start <= ensure_aware_utc(trade.entry_time) < day_end)
        if is_target_day_trade:
            daily += cost
        if trade.status == "open":
            open_risk += cost
            family_key = _risk_family(candidate, trade)
            scope_key = trade.inning_scope or (candidate.inning_scope if candidate else None) or _risk_scope(family_key)
            family[family_key] = family.get(family_key, Decimal("0.00")) + cost
            scope[scope_key] = scope.get(scope_key, Decimal("0.00")) + cost
            if trade.entry_price < settings.paper_low_price_threshold:
                low_price += cost
    return {
        "daily": daily,
        "open": open_risk,
        "family": family,
        "scope": scope,
        "low_price": low_price,
    }


def _risk_limit(bankroll: Decimal, pct: Decimal) -> Decimal:
    return (bankroll * pct).quantize(Decimal("0.01"))


def _intent_cost(intent: TradeIntent) -> Decimal:
    value = intent.candidate.estimated_total_cost
    if value is not None:
        return Decimal(value).quantize(Decimal("0.01"))
    return (intent.price * Decimal(intent.quantity)).quantize(Decimal("0.01"))


def _adjust_intent_quantity(intent: TradeIntent, quantity: int) -> None:
    candidate = intent.candidate
    intent.quantity = quantity
    one_fee = candidate.one_contract_fee_estimate or _estimate_trade_fee(intent.price, 1) or Decimal("0")
    total_fee = _estimate_trade_fee(intent.price, quantity) or Decimal("0")
    total_cost = ((intent.price * Decimal(quantity)) + total_fee).quantize(Decimal("0.01"))
    one_ev = candidate.one_contract_expected_value or candidate.net_expected_value or Decimal("0")
    sized_ev = ((one_ev + one_fee) * Decimal(quantity) - total_fee).quantize(Decimal("0.000001"))
    candidate.contracts = quantity
    candidate.estimated_cost_per_contract = (intent.price + one_fee).quantize(Decimal("0.000001"))
    candidate.estimated_total_cost = total_cost
    candidate.sized_expected_value = sized_ev
    candidate.total_fee_estimate = total_fee
    sizing = dict(intent.sizing or {})
    original_contracts = int(sizing.get("original_contracts") or intent.quantity)
    sizing.update(
        {
            "original_contracts": original_contracts,
            "pre_aggregate_contracts": intent.quantity,
            "contracts": quantity,
            "reduced_contracts": quantity,
            "estimated_total_cost": float(total_cost),
            "sized_expected_value": float(sized_ev),
            "total_fee_estimate": float(total_fee),
            "adjusted_by_aggregate_risk_cap": True,
        }
    )
    intent.sizing = sizing


def _apply_aggregate_risk_caps(
    session: Session,
    intents: list[TradeIntent],
    *,
    target_date: date,
    day_start: datetime,
    day_end: datetime,
    epoch_id: int | None,
    bankroll: Decimal,
    existing_slate_trades: int = 0,
    max_slate_trades: int | None = None,
    existing_open_positions: int = 0,
    max_open_positions: int | None = None,
    max_new_trades: int | None = None,
    max_early_new_trades: int | None = None,
    existing_low_price_trades: int = 0,
    max_low_price_trades_per_slate: int | None = None,
    max_low_price_trades_per_sweep: int | None = None,
    existing_side_trades: dict[str, int] | None = None,
    max_same_side_trades_per_slate: int | None = None,
) -> tuple[list[TradeIntent], dict[str, int], dict[str, object]]:
    settings = get_settings()
    usage = _daily_and_open_risk_usage(session, target_date, day_start, day_end, epoch_id)
    daily_used = Decimal(usage["daily"])
    open_used = Decimal(usage["open"])
    family_used: dict[str, Decimal] = dict(usage["family"])  # type: ignore[arg-type]
    scope_used: dict[str, Decimal] = dict(usage["scope"])  # type: ignore[arg-type]
    low_price_used = Decimal(usage["low_price"])
    limits = {
        "daily": _risk_limit(bankroll, settings.paper_max_daily_new_risk_pct),
        "open": _risk_limit(bankroll, settings.paper_max_open_risk_pct),
        "family": _risk_limit(bankroll, settings.paper_max_market_family_risk_pct),
        "scope": _risk_limit(bankroll, settings.paper_max_scope_risk_pct),
        "low_price": _risk_limit(bankroll, settings.paper_max_price_bucket_risk_pct_under_20c),
    }
    cap_counts = {
        "no_trade_daily_risk_cap": 0,
        "no_trade_open_risk_cap": 0,
        "no_trade_family_risk_cap": 0,
        "no_trade_scope_risk_cap": 0,
        "no_trade_low_price_bucket_risk_cap": 0,
        "no_trade_post_cap_size_too_small": 0,
        "no_trade_slate_cap": 0,
        "no_trade_open_position_cap": 0,
        "no_trade_sweep_cap_reached": 0,
        "no_trade_time_bucket_reserve": 0,
        "no_trade_low_price_slate_cap": 0,
        "no_trade_low_price_sweep_cap": 0,
        "no_trade_side_concentration_cap": 0,
        "aggregate_risk_quantity_reduced": 0,
    }
    selected: list[TradeIntent] = []
    selected_low_price = 0
    side_existing = existing_side_trades or {}
    selected_side_counts: dict[str, int] = {}

    for intent in intents:
        candidate = intent.candidate
        family = candidate.market_family or "unknown"
        scope = candidate.inning_scope or _risk_scope(family)
        side = (candidate.contract_side or "unknown").lower()
        low_price_trade = intent.price < settings.paper_low_price_threshold
        cost = _intent_cost(intent)
        available = {
            "no_trade_daily_risk_cap": limits["daily"] - daily_used,
            "no_trade_open_risk_cap": limits["open"] - open_used,
            "no_trade_family_risk_cap": limits["family"] - family_used.get(family, Decimal("0.00")),
            "no_trade_scope_risk_cap": limits["scope"] - scope_used.get(scope, Decimal("0.00")),
        }
        if low_price_trade:
            available["no_trade_low_price_bucket_risk_cap"] = limits["low_price"] - low_price_used
        blocking_reason = next((reason for reason, remaining in available.items() if remaining < cost), None)
        if blocking_reason:
            cost_per_contract = candidate.estimated_cost_per_contract or (intent.price + (_estimate_trade_fee(intent.price, 1) or Decimal("0")))
            remaining = min(available.values())
            adjusted_quantity = int((max(remaining, Decimal("0")) / cost_per_contract).to_integral_value(rounding=ROUND_FLOOR))
            adjusted_quantity = min(adjusted_quantity, intent.quantity)
            if adjusted_quantity >= settings.paper_min_contracts:
                _adjust_intent_quantity(intent, adjusted_quantity)
                cost = _intent_cost(intent)
                cap_counts["aggregate_risk_quantity_reduced"] += 1
            else:
                candidate.decision = blocking_reason
                _update_gate(candidate, "gate_caps_ok", False)
                cap_counts[blocking_reason] = cap_counts.get(blocking_reason, 0) + 1
                session.add(candidate)
                continue

        if _reject_post_cap_size_if_needed(session, intent):
            cap_counts["no_trade_post_cap_size_too_small"] += 1
            continue

        if max_slate_trades is not None and existing_slate_trades + len(selected) >= max_slate_trades:
            candidate.decision = "no_trade_slate_cap"
            _update_gate(candidate, "gate_caps_ok", False)
            cap_counts["no_trade_slate_cap"] += 1
            session.add(candidate)
            continue

        if max_open_positions is not None and existing_open_positions + len(selected) >= max_open_positions:
            candidate.decision = "no_trade_open_position_cap"
            _update_gate(candidate, "gate_open_position_ok", False)
            cap_counts["no_trade_open_position_cap"] += 1
            session.add(candidate)
            continue

        if max_new_trades is not None and len(selected) >= max_new_trades:
            candidate.decision = "no_trade_sweep_cap_reached"
            _update_gate(candidate, "gate_caps_ok", False)
            cap_counts["no_trade_sweep_cap_reached"] += 1
            session.add(candidate)
            continue

        if max_early_new_trades is not None and len(selected) >= max_early_new_trades:
            candidate.decision = "no_trade_time_bucket_reserve"
            _update_gate(candidate, "gate_caps_ok", False)
            cap_counts["no_trade_time_bucket_reserve"] += 1
            session.add(candidate)
            continue

        if (
            low_price_trade
            and max_low_price_trades_per_slate is not None
            and existing_low_price_trades + selected_low_price >= max_low_price_trades_per_slate
        ):
            candidate.decision = "no_trade_low_price_slate_cap"
            _update_gate(candidate, "gate_caps_ok", False)
            cap_counts["no_trade_low_price_slate_cap"] += 1
            session.add(candidate)
            continue

        if (
            low_price_trade
            and max_low_price_trades_per_sweep is not None
            and selected_low_price >= max_low_price_trades_per_sweep
        ):
            candidate.decision = "no_trade_low_price_sweep_cap"
            _update_gate(candidate, "gate_caps_ok", False)
            cap_counts["no_trade_low_price_sweep_cap"] += 1
            session.add(candidate)
            continue

        if (
            side in {"yes", "no"}
            and max_same_side_trades_per_slate is not None
            and side_existing.get(side, 0) + selected_side_counts.get(side, 0) >= max_same_side_trades_per_slate
        ):
            candidate.decision = "no_trade_side_concentration_cap"
            _update_gate(candidate, "gate_caps_ok", False)
            cap_counts["no_trade_side_concentration_cap"] += 1
            session.add(candidate)
            continue

        candidate.decision = "paper_trade"
        _update_gate(candidate, "gate_caps_ok", True)
        selected.append(intent)
        daily_used += cost
        open_used += cost
        family_used[family] = family_used.get(family, Decimal("0.00")) + cost
        scope_used[scope] = scope_used.get(scope, Decimal("0.00")) + cost
        if low_price_trade:
            low_price_used += cost
            selected_low_price += 1
        if side in {"yes", "no"}:
            selected_side_counts[side] = selected_side_counts.get(side, 0) + 1

    summary = {
        "risk_limit_basis_type": "active_epoch_portfolio_value",
        "risk_limit_basis_amount": float(bankroll),
        "risk_limit_max_at_sweep": {key: float(value) for key, value in limits.items()},
        "daily_risk_used": float(daily_used),
        "daily_risk_max": float(limits["daily"]),
        "open_risk_used": float(open_used),
        "open_risk_max": float(limits["open"]),
        "family_risk_used": {key: float(value) for key, value in family_used.items()},
        "family_risk_max": float(limits["family"]),
        "scope_risk_used": {key: float(value) for key, value in scope_used.items()},
        "scope_risk_max": float(limits["scope"]),
        "low_price_bucket_risk_used": float(low_price_used),
        "low_price_bucket_risk_max": float(limits["low_price"]),
        "existing_slate_trades": existing_slate_trades,
        "max_slate_trades": max_slate_trades,
        "existing_open_positions": existing_open_positions,
        "max_open_positions": max_open_positions,
        "max_new_trades_per_sweep": max_new_trades,
        "max_early_new_trades": max_early_new_trades,
        "existing_low_price_trades": existing_low_price_trades,
        "max_low_price_trades_per_slate": max_low_price_trades_per_slate,
        "max_low_price_trades_per_sweep": max_low_price_trades_per_sweep,
        "side_existing_slate": side_existing,
        "side_new_after_sizing_and_risk": selected_side_counts,
        "max_same_side_trades_per_slate": max_same_side_trades_per_slate,
        "new_trades_after_sizing_and_risk": len(selected),
        "low_price_new_after_sizing_and_risk": selected_low_price,
    }
    return selected, cap_counts, summary


def _reject_post_cap_size_if_needed(session: Session, intent: TradeIntent) -> bool:
    settings = get_settings()
    estimated_total_cost = _intent_cost(intent)
    if (
        intent.quantity >= settings.paper_min_post_cap_contracts
        and estimated_total_cost >= settings.paper_min_post_cap_notional
    ):
        return False

    candidate = intent.candidate
    candidate.decision = "no_trade_post_cap_size_too_small"
    _update_gate(candidate, "gate_caps_ok", False)
    original_contracts = int((intent.sizing or {}).get("original_contracts") or intent.quantity)
    post_cap_size = {
        "original_intended_contracts": original_contracts,
        "final_contracts": intent.quantity,
        "estimated_total_cost": float(estimated_total_cost),
        "min_post_cap_contracts": settings.paper_min_post_cap_contracts,
        "min_post_cap_notional": float(settings.paper_min_post_cap_notional),
        "rejection_reason": candidate.decision,
    }
    sizing = dict(intent.sizing or {})
    sizing["post_cap_size"] = post_cap_size
    intent.sizing = sizing
    candidate.scoring_rationale = {
        **(candidate.scoring_rationale or {}),
        "post_cap_size": post_cap_size,
    }
    _update_candidate_diagnostics(candidate, {"post_cap_size": post_cap_size})
    session.add(candidate)
    return True


def _apply_post_cap_size_guard(
    session: Session,
    intents: list[TradeIntent],
) -> tuple[list[TradeIntent], dict[str, int]]:
    selected: list[TradeIntent] = []
    counts = {"no_trade_post_cap_size_too_small": 0}
    for intent in intents:
        if _reject_post_cap_size_if_needed(session, intent):
            counts["no_trade_post_cap_size_too_small"] += 1
            continue
        selected.append(intent)
    return selected, counts


def _low_price_control_summary(candidates: list[ModelCandidate], decision_counts: dict[str, int]) -> dict[str, object]:
    settings = get_settings()
    low_price_candidates = [
        candidate
        for candidate in candidates
        if candidate.executable_price is not None
        and candidate.executable_price >= settings.paper_min_trade_price
        and candidate.executable_price < settings.paper_low_price_threshold
    ]
    return {
        "paper_min_trade_price": float(settings.paper_min_trade_price),
        "paper_low_price_threshold": float(settings.paper_low_price_threshold),
        "paper_low_price_min_net_ev": float(settings.paper_low_price_min_net_ev),
        "paper_low_price_min_prob_edge": float(settings.paper_low_price_min_prob_edge),
        "paper_low_price_max_trades_per_slate": settings.paper_low_price_max_trades_per_slate,
        "paper_low_price_max_trades_per_sweep": settings.paper_low_price_max_trades_per_sweep,
        "low_price_candidates_considered": len(low_price_candidates),
        "rejected_price_below_floor": decision_counts.get("no_trade_price_below_floor", 0),
        "rejected_low_price_probability_edge": decision_counts.get("no_trade_low_price_probability_edge_low", 0),
        "rejected_low_price_net_ev": decision_counts.get("no_trade_low_price_ev_too_low", 0),
        "rejected_low_price_slate_cap": decision_counts.get("no_trade_low_price_slate_cap", 0),
        "rejected_low_price_sweep_cap": decision_counts.get("no_trade_low_price_sweep_cap", 0),
    }


def _avg_decimal(values: list[Decimal]) -> float | None:
    if not values:
        return None
    return float(sum(values) / Decimal(len(values)))


def _max_decimal(values: list[Decimal]) -> float | None:
    if not values:
        return None
    return float(max(values))


def _min_decimal(values: list[Decimal]) -> float | None:
    if not values:
        return None
    return float(min(values))


def _decision_breakdowns(candidates: list[ModelCandidate]) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    by_family: dict[str, dict[str, int]] = {}
    by_scope: dict[str, dict[str, int]] = {}
    for candidate in candidates:
        family = candidate.market_family or candidate.market_type or "unknown"
        scope = candidate.inning_scope or _scope_for_family(family)
        decision = candidate.decision or "unknown"
        family_bucket = by_family.setdefault(family, {})
        family_bucket[decision] = family_bucket.get(decision, 0) + 1
        scope_bucket = by_scope.setdefault(scope, {})
        scope_bucket[decision] = scope_bucket.get(decision, 0) + 1
    return by_family, by_scope


def _gate_summary(candidates: list[ModelCandidate]) -> dict[str, object]:
    def count(key: str, expected: bool = True) -> int:
        return sum(1 for candidate in candidates if bool((candidate.gate_diagnostics or {}).get(key)) is expected)

    data_quality_values = [candidate.data_quality for candidate in candidates if candidate.data_quality is not None]
    raw_data_quality_values = [
        _quality_decimal((candidate.gate_diagnostics or {}).get("raw_feature_snapshot_data_quality"))
        for candidate in candidates
        if (candidate.gate_diagnostics or {}).get("raw_feature_snapshot_data_quality") is not None
    ]
    ev_passing_values = [
        candidate.net_expected_value
        for candidate in candidates
        if candidate.net_expected_value is not None and bool((candidate.gate_diagnostics or {}).get("gate_net_ev_ok"))
    ]
    edge_passing_values = [
        candidate.probability_edge
        for candidate in candidates
        if candidate.probability_edge is not None and bool((candidate.gate_diagnostics or {}).get("gate_probability_edge_ok"))
    ]
    market_context_status_counts: dict[str, int] = {}
    quality_block_reason_counts: dict[str, int] = {}
    quality_penalty_totals: dict[str, Decimal] = {}
    quality_bucket_counts: dict[str, int] = {"below_threshold": 0, "meets_threshold": 0}
    threshold = _paper_quality_threshold()
    for candidate in candidates:
        diagnostics = candidate.gate_diagnostics or {}
        status = str(diagnostics.get("candidate_stage_market_context_status") or "unknown")
        market_context_status_counts[status] = market_context_status_counts.get(status, 0) + 1
        if candidate.data_quality is not None and candidate.data_quality >= threshold:
            quality_bucket_counts["meets_threshold"] += 1
        else:
            quality_bucket_counts["below_threshold"] += 1
        for reason in diagnostics.get("quality_block_reason") or []:
            reason_key = str(reason)
            quality_block_reason_counts[reason_key] = quality_block_reason_counts.get(reason_key, 0) + 1
        quality = diagnostics.get("quality_decomposition")
        if isinstance(quality, dict):
            paper = quality.get("paper_observation")
            penalties = paper.get("quality_penalty_by_module") if isinstance(paper, dict) else {}
            if isinstance(penalties, dict):
                for module_name, value in penalties.items():
                    quality_penalty_totals[str(module_name)] = quality_penalty_totals.get(
                        str(module_name), Decimal("0")
                    ) + _quality_decimal(value)

    top_quality_blockers = sorted(
        (
            {"module": module_name, "total_penalty": float(value.quantize(Decimal("0.0001")))}
            for module_name, value in quality_penalty_totals.items()
            if value > Decimal("0")
        ),
        key=lambda item: item["total_penalty"],
        reverse=True,
    )[:8]
    return {
        "trade_eligible_before_quality": count("counterfactual_trade_eligible_before_quality"),
        "trade_eligible_after_quality": count("counterfactual_trade_eligible_after_quality"),
        "blocked_by_quality_only": count("blocked_by_quality_only"),
        "would_pass_ev_if_quality_allowed": count("would_pass_ev_if_quality_allowed"),
        "would_pass_edge_if_quality_allowed": count("would_pass_edge_if_quality_allowed"),
        "ev_edge_pass_but_quality_fail": count("ev_edge_pass_but_quality_fail"),
        "blocked_by_ev": count("gate_net_ev_ok", False),
        "blocked_by_edge": count("gate_probability_edge_ok", False),
        "blocked_by_price": count("gate_price_fresh_executable", False),
        "blocked_by_mapping": count("gate_mapping_ok", False),
        "blocked_by_push": count("gate_push_ok", False),
        "blocked_by_line_selection": count("gate_line_selection_ok", False),
        "blocked_by_caps": count("gate_caps_ok", False),
        "average_data_quality": _avg_decimal(data_quality_values),
        "min_data_quality": _min_decimal(data_quality_values),
        "max_data_quality": _max_decimal(data_quality_values),
        "raw_feature_snapshot_data_quality_avg": _avg_decimal(raw_data_quality_values),
        "raw_feature_snapshot_data_quality_min": _min_decimal(raw_data_quality_values),
        "raw_feature_snapshot_data_quality_max": _max_decimal(raw_data_quality_values),
        "paper_observation_data_quality_avg": _avg_decimal(data_quality_values),
        "paper_observation_data_quality_min": _min_decimal(data_quality_values),
        "paper_observation_data_quality_max": _max_decimal(data_quality_values),
        "quality_threshold": float(threshold),
        "candidate_stage_market_context_status_counts": market_context_status_counts,
        "quality_block_reason_counts": quality_block_reason_counts,
        "quality_bucket_counts": quality_bucket_counts,
        "top_quality_blockers": top_quality_blockers,
        "average_net_ev_among_ev_passing": _avg_decimal(ev_passing_values),
        "average_probability_edge_among_edge_passing": _avg_decimal(edge_passing_values),
    }


def _quality_bucket(candidate: ModelCandidate) -> str:
    if candidate.data_quality is None:
        return "missing"
    threshold = _paper_quality_threshold()
    if candidate.data_quality >= threshold:
        return "meets_threshold"
    if candidate.data_quality >= threshold - Decimal("0.0500"):
        return "near_threshold"
    return "below_threshold"


def _candidate_opportunity_key(candidate: ModelCandidate) -> tuple[int | None, str, str]:
    family = candidate.market_family or candidate.market_type or "unknown"
    scope = candidate.inning_scope or _scope_for_family(family)
    return candidate.mlb_game_id, scope, family


def _candidate_opportunity_side_key(candidate: ModelCandidate) -> tuple[int | None, str, str, str]:
    game_id, scope, family = _candidate_opportunity_key(candidate)
    return game_id, scope, family, (candidate.contract_side or "unknown").lower()


def _candidate_counterfactual_payload(candidate: ModelCandidate) -> dict[str, object]:
    diagnostics = candidate.gate_diagnostics or {}
    return {
        "candidate_id": candidate.id,
        "mlb_game_id": candidate.mlb_game_id,
        "market_ticker": (candidate.features or {}).get("market_context", {}).get("ticker")
        if isinstance(candidate.features, dict)
        else None,
        "market_family": candidate.market_family or candidate.market_type,
        "scope": candidate.inning_scope or _scope_for_family(candidate.market_family or candidate.market_type),
        "contract_side": candidate.contract_side,
        "decision": candidate.decision,
        "net_expected_value": _decimal_json(candidate.net_expected_value),
        "probability_edge": _decimal_json(candidate.probability_edge),
        "paper_observation_data_quality": _decimal_json(candidate.data_quality),
        "raw_feature_snapshot_data_quality": diagnostics.get("raw_feature_snapshot_data_quality"),
        "quality_block_reason": diagnostics.get("quality_block_reason") or [],
    }


def _average_max_by_group(
    candidates: list[ModelCandidate],
    group_fn,
    value_attr: str,
) -> dict[str, dict[str, float | int | None]]:
    grouped: dict[str, list[Decimal]] = {}
    for candidate in candidates:
        value = getattr(candidate, value_attr)
        if value is None:
            continue
        key = str(group_fn(candidate) or "unknown")
        grouped.setdefault(key, []).append(value)
    return {
        key: {
            "count": len(values),
            "avg": _avg_decimal(values),
            "max": _max_decimal(values),
        }
        for key, values in sorted(grouped.items())
    }


def _opportunity_diagnostics(candidates: list[ModelCandidate]) -> dict[str, object]:
    ev_pass = [
        candidate
        for candidate in candidates
        if bool((candidate.gate_diagnostics or {}).get("gate_gross_ev_positive"))
        and bool((candidate.gate_diagnostics or {}).get("gate_net_ev_ok"))
    ]
    edge_pass = [
        candidate for candidate in candidates if bool((candidate.gate_diagnostics or {}).get("gate_probability_edge_ok"))
    ]
    ev_and_edge_pass = [
        candidate
        for candidate in candidates
        if candidate in ev_pass and bool((candidate.gate_diagnostics or {}).get("gate_probability_edge_ok"))
    ]
    pre_quality = [
        candidate
        for candidate in candidates
        if bool((candidate.gate_diagnostics or {}).get("counterfactual_trade_eligible_before_quality"))
    ]
    post_quality = [
        candidate
        for candidate in candidates
        if bool((candidate.gate_diagnostics or {}).get("counterfactual_trade_eligible_after_quality"))
    ]
    quality_blocked = [
        candidate for candidate in candidates if bool((candidate.gate_diagnostics or {}).get("blocked_by_quality_only"))
    ]
    top_quality_blocked = sorted(
        quality_blocked,
        key=lambda candidate: (
            candidate.net_expected_value or Decimal("-999"),
            candidate.probability_edge or Decimal("-999"),
        ),
        reverse=True,
    )[:10]

    deduped_top: dict[tuple[int | None, str, str], ModelCandidate] = {}
    for candidate in sorted(
        quality_blocked,
        key=lambda item: (item.net_expected_value or Decimal("-999"), item.probability_edge or Decimal("-999")),
        reverse=True,
    ):
        deduped_top.setdefault(_candidate_opportunity_key(candidate), candidate)

    family_counts: dict[str, dict[str, int]] = {}
    scope_counts: dict[str, dict[str, int]] = {}
    side_counts: dict[str, dict[str, int]] = {}
    quality_bucket_counts: dict[str, int] = {}
    for candidate in candidates:
        decision = candidate.decision or "unknown"
        family = candidate.market_family or candidate.market_type or "unknown"
        scope = candidate.inning_scope or _scope_for_family(family)
        side = (candidate.contract_side or "unknown").lower()
        family_bucket = family_counts.setdefault(family, {})
        family_bucket[decision] = family_bucket.get(decision, 0) + 1
        scope_bucket = scope_counts.setdefault(scope, {})
        scope_bucket[decision] = scope_bucket.get(decision, 0) + 1
        side_bucket = side_counts.setdefault(side, {})
        side_bucket[decision] = side_bucket.get(decision, 0) + 1
        bucket = _quality_bucket(candidate)
        quality_bucket_counts[bucket] = quality_bucket_counts.get(bucket, 0) + 1

    return {
        "candidates_total": len(candidates),
        "ev_pass_count": len(ev_pass),
        "edge_pass_count": len(edge_pass),
        "ev_and_edge_pass_count": len(ev_and_edge_pass),
        "pre_quality_trade_eligible_count": len(pre_quality),
        "post_quality_trade_eligible_count": len(post_quality),
        "quality_blocked_count": len(quality_blocked),
        "mapping_uncertain_count": sum(1 for candidate in candidates if candidate.decision == "no_trade_mapping_uncertain"),
        "unique_game_count": len({candidate.mlb_game_id for candidate in candidates}),
        "unique_game_scope_count": len({(candidate.mlb_game_id, candidate.inning_scope or _scope_for_family(candidate.market_family or candidate.market_type)) for candidate in candidates}),
        "unique_game_scope_family_count": len({_candidate_opportunity_key(candidate) for candidate in candidates}),
        "unique_game_scope_family_side_count": len({_candidate_opportunity_side_key(candidate) for candidate in candidates}),
        "deduped_ev_edge_pass_count_by_game_scope_family": len(
            {_candidate_opportunity_key(candidate) for candidate in ev_and_edge_pass}
        ),
        "deduped_pre_quality_trade_eligible_count_by_game_scope_family": len(
            {_candidate_opportunity_key(candidate) for candidate in pre_quality}
        ),
        "top_counterfactual_candidates_blocked_by_quality": [
            _candidate_counterfactual_payload(candidate) for candidate in top_quality_blocked
        ],
        "top_deduped_counterfactual_opinions_by_game_scope_family": [
            _candidate_counterfactual_payload(candidate) for candidate in list(deduped_top.values())[:10]
        ],
        "avg_max_net_ev_by_family": _average_max_by_group(
            candidates, lambda candidate: candidate.market_family or candidate.market_type, "net_expected_value"
        ),
        "avg_max_net_ev_by_scope": _average_max_by_group(
            candidates, lambda candidate: candidate.inning_scope or _scope_for_family(candidate.market_family or candidate.market_type), "net_expected_value"
        ),
        "avg_max_net_ev_by_side": _average_max_by_group(
            candidates, lambda candidate: (candidate.contract_side or "unknown").lower(), "net_expected_value"
        ),
        "avg_max_probability_edge_by_family": _average_max_by_group(
            candidates, lambda candidate: candidate.market_family or candidate.market_type, "probability_edge"
        ),
        "avg_max_probability_edge_by_scope": _average_max_by_group(
            candidates, lambda candidate: candidate.inning_scope or _scope_for_family(candidate.market_family or candidate.market_type), "probability_edge"
        ),
        "avg_max_probability_edge_by_side": _average_max_by_group(
            candidates, lambda candidate: (candidate.contract_side or "unknown").lower(), "probability_edge"
        ),
        "counts_by_decision_family": family_counts,
        "counts_by_decision_scope": scope_counts,
        "counts_by_decision_side": side_counts,
        "counts_by_quality_bucket": quality_bucket_counts,
    }


def _zero_trade_reason(decision_counts: dict[str, int]) -> str | None:
    if not decision_counts:
        return "no_candidates_evaluated"
    blocked = {reason: count for reason, count in decision_counts.items() if reason != "paper_trade"}
    if not blocked:
        return None
    return max(blocked.items(), key=lambda item: item[1])[0]


def generate_candidates(
    session: Session,
    target_date: date | None = None,
    *,
    min_time_to_start_minutes: int | None = None,
    max_time_to_start_minutes: int | None = None,
    sweep_label: str | None = None,
    dry_run_candidates_only: bool = False,
) -> dict[str, object]:
    _validate_sweep_window(min_time_to_start_minutes, max_time_to_start_minutes)
    settings = get_settings()
    now = utc_now()
    day, day_start, day_end = _candidate_day_bounds(now, target_date)
    normalized_sweep_label = sweep_label.strip() if isinstance(sweep_label, str) and sweep_label.strip() else None
    sweep_summary, in_window_game_ids = _sweep_window_summary(
        session,
        target_date=day,
        day_start=day_start,
        day_end=day_end,
        now=now,
        min_minutes=min_time_to_start_minutes,
        max_minutes=max_time_to_start_minutes,
        sweep_label=normalized_sweep_label,
        dry_run_candidates_only=dry_run_candidates_only,
    )
    sweep_window_enabled = bool(sweep_summary["sweep_window_enabled"])
    active_epoch = get_or_create_active_paper_epoch(session)
    created_or_updated = 0
    paper_trades = 0
    model_version = get_or_create_mature_model_version(session)
    parameter_version = get_or_create_active_parameter_version(session)
    prediction_run = ModelPredictionRun(
        paper_trading_epoch_id=active_epoch.id,
        started_at=now,
        target_date=day,
        status="running",
        model_version_tag=MATURE_MODEL_TAG,
        feature_version=FEATURE_VERSION,
        trade_policy={
            "paper_max_trades_per_slate": settings.paper_max_trades_per_slate,
            "paper_max_trades_per_game": settings.paper_max_trades_per_game,
            "paper_max_trades_per_market_family": settings.paper_max_trades_per_market_family,
            "paper_max_trades_per_game_family": settings.paper_max_trades_per_game_family,
            "paper_max_trades_per_game_scope": settings.paper_max_trades_per_game_scope,
            "paper_max_open_positions": settings.paper_max_open_positions,
            "paper_min_net_ev": float(settings.paper_min_net_ev),
            "paper_min_prob_edge": float(settings.paper_min_prob_edge),
            "paper_min_data_quality": float(settings.paper_min_data_quality),
            "paper_observation_min_data_quality": float(settings.paper_observation_min_data_quality),
            "live_min_data_quality": float(settings.live_min_data_quality),
            "paper_require_calibrated_for_trade": settings.paper_require_calibrated_for_trade,
            "paper_max_price_staleness_seconds": settings.paper_max_price_staleness_seconds,
            "paper_allow_last_price_fallback_for_trade": settings.paper_allow_last_price_fallback_for_trade,
            "paper_allow_multiple_lines_per_game_family": settings.paper_allow_multiple_lines_per_game_family,
            "paper_allow_multiple_f5_winner_outcomes": settings.paper_allow_multiple_f5_winner_outcomes,
            "paper_selector_mode": settings.paper_selector_mode,
            "selector_policy_version": SELECTOR_POLICY_VERSION,
            "probability_adapter_policy_version": PROBABILITY_ADAPTER_POLICY_VERSION,
            "paper_spread_trading_enabled": settings.paper_spread_trading_enabled,
            "paper_first_five_spread_trading_enabled": settings.paper_spread_trading_enabled,
            "paper_full_game_spread_trading_enabled": settings.paper_full_game_spread_trading_enabled,
            "full_game_spread_audit_gate_enabled": True,
            "full_game_spread_requires_trusted_audit": True,
            "full_game_spread_audit_only": not settings.paper_full_game_spread_trading_enabled,
            "paper_f5_tie_trading_enabled": False,
            "paper_min_trade_price": float(settings.paper_min_trade_price),
            "paper_low_price_threshold": float(settings.paper_low_price_threshold),
            "paper_low_price_max_trades_per_slate": settings.paper_low_price_max_trades_per_slate,
            "paper_low_price_max_trades_per_sweep": settings.paper_low_price_max_trades_per_sweep,
            "paper_low_price_min_net_ev": float(settings.paper_low_price_min_net_ev),
            "paper_low_price_min_prob_edge": float(settings.paper_low_price_min_prob_edge),
            "paper_min_post_cap_contracts": settings.paper_min_post_cap_contracts,
            "paper_min_post_cap_notional": float(settings.paper_min_post_cap_notional),
            "paper_max_new_trades_per_sweep": settings.paper_max_new_trades_per_sweep,
            "paper_max_new_trades_before_3pm_et": settings.paper_max_new_trades_before_3pm_et,
            "paper_reserve_trades_after_3pm_et": settings.paper_reserve_trades_after_3pm_et,
            "paper_max_same_side_trades_per_slate": settings.paper_max_same_side_trades_per_slate,
            "side_aware_candidates_enabled": True,
            "aggregate_risk_caps_enabled": True,
            "paper_max_daily_new_risk_pct": float(settings.paper_max_daily_new_risk_pct),
            "paper_max_open_risk_pct": float(settings.paper_max_open_risk_pct),
            "paper_max_market_family_risk_pct": float(settings.paper_max_market_family_risk_pct),
            "paper_max_scope_risk_pct": float(settings.paper_max_scope_risk_pct),
            "paper_max_price_bucket_risk_pct_under_20c": float(settings.paper_max_price_bucket_risk_pct_under_20c),
            "kalshi_trade_fee_rate": float(settings.kalshi_trade_fee_rate),
            "kalshi_fee_estimate_mode": settings.kalshi_fee_estimate_mode,
            "kalshi_fee_rounding_mode": settings.kalshi_fee_rounding_mode,
            "kalshi_assume_taker": settings.kalshi_assume_taker,
            "paper_position_sizing_mode": settings.paper_position_sizing_mode,
            "paper_risk_per_trade_pct": float(settings.paper_risk_per_trade_pct),
            "paper_min_contracts": settings.paper_min_contracts,
            "paper_max_contracts_per_trade": settings.paper_max_contracts_per_trade,
            "sweep_label": normalized_sweep_label,
            "sweep_window_enabled": sweep_window_enabled,
            "min_time_to_start_minutes": min_time_to_start_minutes,
            "max_time_to_start_minutes": max_time_to_start_minutes,
            "dry_run_candidates_only": dry_run_candidates_only,
        },
    )
    session.add(prediction_run)
    session.flush()

    mapping_query = (
        select(MarketMapping, MlbGame, KalshiMarket)
        .join(MlbGame, MarketMapping.mlb_game_id == MlbGame.id)
        .join(KalshiMarket, MarketMapping.kalshi_market_id == KalshiMarket.id)
        .where(MarketMapping.mapping_status.in_(["candidate", "confirmed", "needs_review"]))
        .where(func.lower(MlbGame.status).in_(PLAYABLE_GAME_STATUSES))
    )
    if sweep_window_enabled:
        source_mappings = session.execute(
            mapping_query
            .where(MlbGame.scheduled_start >= day_start - timedelta(days=1))
            .where(MlbGame.scheduled_start < day_end + timedelta(days=1))
        ).all()
        mappings = [
            (mapping, game, market)
            for mapping, game, market in source_mappings
            if game.id is not None and game.id in in_window_game_ids
        ]
    else:
        mappings = session.execute(
            mapping_query
            .where(MlbGame.scheduled_start > now)
            .where(MlbGame.scheduled_start >= day_start)
            .where(MlbGame.scheduled_start < day_end)
        ).all()
        source_mappings = mappings
    warnings: list[str] = []
    if not mappings:
        warnings.append("no_candidates_missing_mappings: run Kalshi market discovery and mapping sync for this target date.")
    if sweep_window_enabled and not in_window_game_ids:
        warnings.append("skipped_no_games_in_window: no target-date games matched the requested time-to-start window.")

    trade_intents: list[TradeIntent] = []
    evaluated_candidates: list[ModelCandidate] = []
    outputs_by_candidate_id: dict[int, ModelPredictionOutput] = {}
    open_trade_metadata_refreshes: list[tuple[ModelCandidate, PaperTrade]] = []
    stale_price_count = 0
    non_executable_price_count = 0

    for mapping, game, market in mappings:
        minutes_to_start = int((ensure_aware_utc(game.scheduled_start) - now).total_seconds() / 60)
        bucket = classify_time_bucket(minutes_to_start)
        market_type = (
            mapping.market_type
            or market.market_type
            or market_type_from_ticker(market.ticker, infer_market_type(_market_classification_text(market)))
        )
        spread_verification = _spread_verification_for_candidate_gate(
            game=game,
            mapping=mapping,
            market=market,
            market_type=market_type,
        )
        spread_audit_metadata = _compact_spread_audit_metadata(spread_verification)
        parsed_selection_code = (
            spread_verification.selection_code
            if spread_verification and spread_verification.selection_code
            else mapping.selection_code or market.selection_code
        )
        parsed_line_value = (
            spread_verification.line_value
            if spread_verification and spread_verification.line_value is not None
            else mapping.line_value if mapping.line_value is not None else market.line_value
        )
        base_features = build_feature_snapshot(game, market, mapping, session=session, now=now)
        model_score = score_mature_candidate(
            base_features,
            market_type=market_type,
            settlement_status=_settlement_status(mapping, market),
            parameters=parameter_version.parameters,
            parameter_version_tag=parameter_version.version_tag,
        )
        actual_yes_probability = model_score.probability_calibrated or model_score.probability
        actual_no_probability = (Decimal("1.000000") - actual_yes_probability).quantize(Decimal("0.000001"))
        price_contexts = {"yes": _market_side_price_context(market, "yes", now)}
        no_price_context = _market_side_price_context(market, "no", now)
        if no_price_context.market_price is not None:
            price_contexts["no"] = no_price_context

        for contract_side, price_context in price_contexts.items():
            price = price_context.executable_price
            features = {**base_features}
            market_context = dict(features.get("market_context") or {})
            market_context["contract_side"] = contract_side
            market_context["executable_price"] = float(price) if price is not None else None
            market_context["executable_price_source"] = price_context.source
            market_context["market_price"] = float(price_context.market_price) if price_context.market_price is not None else None
            market_context["price_status"] = price_context.status
            market_context["price_staleness_seconds"] = price_context.staleness_seconds
            market_context["sweep_label"] = normalized_sweep_label
            market_context["sweep_window_enabled"] = sweep_window_enabled
            if spread_audit_metadata is not None:
                market_context["spread_verification"] = spread_audit_metadata
            features["market_context"] = market_context
            labels = contract_labels(
                game=game,
                market=market,
                market_ticker=market.ticker,
                market_type=market_type,
                selection_code=parsed_selection_code,
                contract_side=contract_side,
            )
            actual_display = labels.actual_contract_display or labels.contract_display
            exposure_taxonomy = exposure_taxonomy_for_candidate(
                game=game,
                market_family=mapping.market_family or market.market_family or market_type,
                selection_code=parsed_selection_code,
                line_value=parsed_line_value,
                over_under_side=mapping.over_under_side or market.over_under_side,
                contract_side=contract_side,
                contract_mechanics_label=actual_display,
                spread_verification=spread_verification,
            )
            market_context["exposure_taxonomy"] = exposure_taxonomy.as_dict()
            features["exposure_taxonomy"] = exposure_taxonomy.as_dict()
            adapter_result = score_probability_adapter(
                ProbabilityAdapterContext(
                    features=features,
                    market_type=market_type,
                    contract_side=contract_side,
                    settlement_status=_settlement_status(mapping, market),
                    parameters=parameter_version.parameters,
                    parameter_version_tag=parameter_version.version_tag,
                    exposure_taxonomy=exposure_taxonomy,
                    base_model_score=model_score,
                    spread_verification=spread_verification,
                )
            )
            probability = adapter_result.probability
            probability_raw = adapter_result.probability_raw
            fair_value = adapter_result.fair_value
            actual_yes_probability = (
                probability
                if contract_side == "yes"
                else (Decimal("1.000000") - probability).quantize(Decimal("0.000001"))
            )
            actual_no_probability = (
                probability
                if contract_side == "no"
                else (Decimal("1.000000") - probability).quantize(Decimal("0.000001"))
            )
            adapter_payload = probability_adapter_candidate_payload(adapter_result)
            market_context["side_probability"] = float(probability)
            market_context["actual_yes_probability"] = float(actual_yes_probability)
            market_context["actual_no_probability"] = float(actual_no_probability)
            market_context["probability_adapter"] = adapter_result.compact_metadata()
            features["probability_adapter"] = adapter_result.compact_metadata()
            candidate_stage_market_context = _candidate_stage_market_context(
                mapping=mapping,
                game=game,
                market=market,
                market_type=market_type,
                price_context=price_context,
                contract_side=contract_side,
            )
            quality_context = _paper_observation_quality_context(
                features=features,
                market_type=market_type,
                candidate_stage_market_context=candidate_stage_market_context,
                model_score_data_quality=adapter_result.data_quality,
            )
            features["raw_feature_snapshot_data_quality"] = (
                float(quality_context.raw_feature_snapshot_data_quality)
                if quality_context.raw_feature_snapshot_data_quality is not None
                else None
            )
            features["paper_observation_data_quality"] = float(quality_context.paper_observation_data_quality)
            features["candidate_stage_market_context"] = quality_context.candidate_stage_market_context
            features["quality_decomposition"] = quality_context.decomposition
            features["quality_block_reason"] = quality_context.quality_block_reason
            features["data_quality"] = float(quality_context.paper_observation_data_quality)
            features["data_quality_summary"] = {
                **(features.get("data_quality_summary") if isinstance(features.get("data_quality_summary"), dict) else {}),
                "raw_feature_snapshot_data_quality": (
                    float(quality_context.raw_feature_snapshot_data_quality)
                    if quality_context.raw_feature_snapshot_data_quality is not None
                    else None
                ),
                "paper_observation_data_quality": float(quality_context.paper_observation_data_quality),
                "quality_threshold": float(quality_context.threshold),
                "quality_block_reason": quality_context.quality_block_reason,
                "candidate_stage_market_context_status": candidate_stage_market_context["source_status"],
                "paper_observation": quality_context.decomposition["paper_observation"],
            }
            gross_ev, fee, net_ev, probability_edge = _expected_values(probability, price, 1)
            market_context["fee_estimate"] = float(fee) if fee is not None else None
            if price_context.status == "stale":
                stale_price_count += 1
            elif price_context.status not in {"fresh_executable", "missing"}:
                non_executable_price_count += 1
            decision = _base_decision(
                mapping,
                game,
                market,
                market_type,
                day,
                minutes_to_start,
                price_context,
                probability,
                gross_ev,
                fee,
                net_ev,
                probability_edge,
                quality_context.paper_observation_data_quality,
                adapter_result.calibration_status,
                adapter_result.push_probability,
            )

            existing_candidates = list(
                session.scalars(
                    select(ModelCandidate)
                    .where(ModelCandidate.mapping_id == mapping.id)
                    .where(ModelCandidate.paper_trading_epoch_id == active_epoch.id)
                    .where(ModelCandidate.time_bucket == bucket)
                    .where(ModelCandidate.target_date == day)
                    .where(ModelCandidate.contract_side == contract_side)
                    .order_by(ModelCandidate.evaluated_at.desc(), ModelCandidate.id.desc())
                )
            )
            traded_candidate_ids = _candidate_ids_with_trades(
                session, [candidate.id for candidate in existing_candidates if candidate.id is not None], active_epoch.id
            )
            existing = next(
                (candidate for candidate in existing_candidates if candidate.id not in traded_candidate_ids),
                None,
            )
            open_trade_for_market = _open_trade_for_market(session, market.ticker, contract_side, active_epoch.id)
            open_trade_for_ticker = _open_trade_for_ticker(session, market.ticker, active_epoch.id)
            opposite_open_trade = (
                open_trade_for_ticker
                if open_trade_for_ticker is not None and open_trade_for_ticker.contract_side != contract_side
                else None
            )
            candidate = existing or ModelCandidate(
                paper_trading_epoch_id=active_epoch.id,
                mapping_id=mapping.id,
                mlb_game_id=game.id,
                kalshi_market_id=market.id,
                evaluated_at=now,
            )
            candidate.paper_trading_epoch_id = active_epoch.id
            if open_trade_for_market is not None and not dry_run_candidates_only:
                mark_price = _open_trade_mark_price(market, contract_side)
                if mark_price is not None:
                    open_trade_for_market.current_price = mark_price
                open_trade_for_market.market_display = open_trade_for_market.market_display or actual_display
                open_trade_for_market.selection_display = open_trade_for_market.selection_display or labels.selection_display
                open_trade_for_market.matchup_display = open_trade_for_market.matchup_display or labels.matchup_display
                open_trade_for_market.contract_display = open_trade_for_market.contract_display or actual_display
                open_trade_for_market.market_family = open_trade_for_market.market_family or mapping.market_family or market.market_family
                open_trade_for_market.line_value = (
                    open_trade_for_market.line_value
                    if open_trade_for_market.line_value is not None
                    else parsed_line_value
                )
                open_trade_for_market.selection_code = open_trade_for_market.selection_code or parsed_selection_code
                open_trade_for_market.over_under_side = (
                    open_trade_for_market.over_under_side or mapping.over_under_side or market.over_under_side
                )
                open_trade_for_market.inning_scope = open_trade_for_market.inning_scope or mapping.inning_scope or market.inning_scope
                open_trade_for_market.settlement_rule_status = (
                    open_trade_for_market.settlement_rule_status
                    or mapping.settlement_rule_status
                    or market.settlement_rule_status
                )
                session.add(open_trade_for_market)
            eligible_for_intent = decision == "eligible_for_paper_trade" and price is not None
            if decision == "eligible_for_paper_trade":
                if dry_run_candidates_only:
                    decision = "candidate_only_dry_run"
                elif traded_candidate_ids or open_trade_for_market is not None:
                    decision = "candidate_only_existing_trade"
                elif opposite_open_trade is not None:
                    decision = "no_trade_opposite_side_open"

            candidate.model_version_id = model_version.id
            candidate.evaluated_at = now
            candidate.features = features
            candidate.probability = probability
            candidate.model_probability = probability
            candidate.probability_raw = probability_raw
            candidate.probability_calibrated = probability
            candidate.fair_value = fair_value
            candidate.market_price = price_context.market_price
            candidate.executable_price = price
            candidate.expected_value = gross_ev
            candidate.fee_estimate = fee
            candidate.net_expected_value = net_ev
            candidate.probability_edge = probability_edge
            candidate.target_date = day
            candidate.executable_price_source = price_context.source
            candidate.market_price_updated_at = price_context.updated_at
            candidate.price_staleness_seconds = price_context.staleness_seconds
            candidate.price_status = price_context.status
            candidate.market_type = market_type
            candidate.time_bucket = bucket
            candidate.time_to_start_minutes = minutes_to_start
            candidate.contract_side = contract_side
            candidate.decision = decision
            candidate.model_version_tag = MATURE_MODEL_TAG
            candidate.feature_version = FEATURE_VERSION
            candidate.training_eligible = adapter_result.training_eligible
            candidate.training_exclusion_reason = adapter_result.training_exclusion_reason
            if _eastern_date(game.scheduled_start) != day:
                candidate.training_eligible = False
                candidate.training_exclusion_reason = "target_date_mismatch"
            elif minutes_to_start <= 0:
                candidate.training_eligible = False
                candidate.training_exclusion_reason = "candidate_after_game_start"
            elif price_context.status != "fresh_executable":
                candidate.training_eligible = False
                candidate.training_exclusion_reason = f"price_context_{price_context.status}"
            elif fee is None:
                candidate.training_eligible = False
                candidate.training_exclusion_reason = "missing_fee_estimate"
            elif decision == "no_trade_mapping_uncertain":
                candidate.training_eligible = False
                candidate.training_exclusion_reason = "mapping_uncertain"
            elif decision.startswith("no_trade_full_game_spread_"):
                candidate.training_eligible = False
                candidate.training_exclusion_reason = decision.removeprefix("no_trade_")
            elif decision == "no_trade_spread_trading_disabled":
                candidate.training_eligible = False
                candidate.training_exclusion_reason = "spread_trading_disabled"
            elif decision == "no_trade_spread_parser_unverified":
                candidate.training_eligible = False
                candidate.training_exclusion_reason = "spread_parser_unverified"
            elif dry_run_candidates_only:
                candidate.training_eligible = False
                candidate.training_exclusion_reason = "dry_run_candidates_only"
            candidate.data_quality = quality_context.paper_observation_data_quality
            candidate.calibration_status = adapter_result.calibration_status
            candidate.scoring_rationale = {
                **model_score.rationale,
                "probability_raw": float(probability_raw),
                "probability_calibrated": float(probability),
                "push_probability": float(adapter_result.push_probability),
                "calibration_status": adapter_result.calibration_status,
                "probability_adapter": adapter_result.compact_metadata(),
                "probability_adapter_policy_version": PROBABILITY_ADAPTER_POLICY_VERSION,
                "raw_feature_snapshot_data_quality": (
                    float(quality_context.raw_feature_snapshot_data_quality)
                    if quality_context.raw_feature_snapshot_data_quality is not None
                    else None
                ),
                "paper_observation_data_quality": float(quality_context.paper_observation_data_quality),
                "quality_threshold": float(quality_context.threshold),
                "quality_decomposition": quality_context.decomposition,
                "quality_block_reason": quality_context.quality_block_reason,
                "ev_decomposition": _ev_decomposition(
                    probability=probability,
                    price=price,
                    gross_ev=gross_ev,
                    fee_estimate=fee,
                    net_ev=net_ev,
                    probability_edge=probability_edge,
                ),
                "contract_side": contract_side,
                "side_probability": float(probability),
                "actual_yes_probability": float(actual_yes_probability),
                "actual_no_probability": float(actual_no_probability),
                "actual_contract_display": labels.actual_contract_display,
                "normalized_equivalent_display": labels.normalized_equivalent_display,
                "display_title": labels.display_title,
                "display_subtitle": labels.display_subtitle,
                "raw_ticker_display": labels.raw_ticker_display,
                "spread_verification": spread_audit_metadata,
                "exposure_taxonomy": exposure_taxonomy.as_dict(),
                "sweep_label": normalized_sweep_label,
                "sweep_window_enabled": sweep_window_enabled,
                "dry_run_candidates_only": dry_run_candidates_only,
            }
            candidate.market_display = actual_display
            candidate.selection_display = labels.selection_display
            candidate.matchup_display = labels.matchup_display
            candidate.contract_display = actual_display
            candidate.market_family = mapping.market_family or market.market_family or market_type
            candidate.line_value = parsed_line_value
            candidate.selection_code = parsed_selection_code
            candidate.over_under_side = mapping.over_under_side or market.over_under_side
            candidate.inning_scope = mapping.inning_scope or market.inning_scope
            candidate.settlement_rule_status = mapping.settlement_rule_status or market.settlement_rule_status
            _apply_exposure_taxonomy(candidate, exposure_taxonomy)
            _apply_probability_adapter_metadata(candidate, adapter_payload)
            if open_trade_for_market is not None and not dry_run_candidates_only:
                _copy_exposure_metadata_to_trade(open_trade_for_market, candidate)
                open_trade_metadata_refreshes.append((candidate, open_trade_for_market))
            diagnostics = _diagnostics_payload(
                mapping=mapping,
                game=game,
                market=market,
                market_type=market_type,
                target_date=day,
                minutes_to_start=minutes_to_start,
                price_context=price_context,
                probability=probability,
                gross_ev=gross_ev,
                fee_estimate=fee,
                net_ev=net_ev,
                probability_edge=probability_edge,
                data_quality=quality_context.paper_observation_data_quality,
                calibration_status=adapter_result.calibration_status,
                push_probability=adapter_result.push_probability,
                open_trade_exists=open_trade_for_market is not None or opposite_open_trade is not None,
            )
            diagnostics["raw_feature_snapshot_data_quality"] = (
                float(quality_context.raw_feature_snapshot_data_quality)
                if quality_context.raw_feature_snapshot_data_quality is not None
                else None
            )
            diagnostics["paper_observation_data_quality"] = float(quality_context.paper_observation_data_quality)
            diagnostics["quality_threshold"] = float(quality_context.threshold)
            diagnostics["quality_decomposition"] = quality_context.decomposition
            diagnostics["quality_block_reason"] = quality_context.quality_block_reason
            diagnostics["candidate_stage_market_context_status"] = candidate_stage_market_context["source_status"]
            diagnostics["ev_decomposition"] = _ev_decomposition(
                probability=probability,
                price=price,
                gross_ev=gross_ev,
                fee_estimate=fee,
                net_ev=net_ev,
                probability_edge=probability_edge,
            )
            diagnostics["actual_contract_display"] = labels.actual_contract_display
            diagnostics["normalized_equivalent_display"] = labels.normalized_equivalent_display
            diagnostics["display_title"] = labels.display_title
            diagnostics["display_subtitle"] = labels.display_subtitle
            diagnostics["raw_ticker_display"] = labels.raw_ticker_display
            diagnostics["exposure_taxonomy"] = exposure_taxonomy.as_dict()
            diagnostics["probability_adapter"] = adapter_result.compact_metadata()
            if spread_audit_metadata is not None:
                diagnostics["spread_verification"] = spread_audit_metadata
            diagnostics["sweep_label"] = normalized_sweep_label
            diagnostics["sweep_window_enabled"] = sweep_window_enabled
            diagnostics["dry_run_candidates_only"] = dry_run_candidates_only
            if decision in {"candidate_only_existing_trade", "no_trade_opposite_side_open"}:
                diagnostics["gate_open_position_ok"] = False
                diagnostics["gate_final_trade_eligible"] = False
            if decision == "candidate_only_dry_run":
                diagnostics["gate_open_position_ok"] = False
                diagnostics["gate_final_trade_eligible"] = False
            _apply_gate_fields(candidate, diagnostics)
            session.add(candidate)
            session.flush()
            session.add(
                FeatureSnapshot(
                    candidate_id=candidate.id,
                    captured_at=now,
                    features=features,
                    source=FEATURE_VERSION,
                    feature_version=FEATURE_VERSION,
                    source_statuses=features.get("source_statuses"),
                )
            )
            created_or_updated += 1
            evaluated_candidates.append(candidate)

            output = ModelPredictionOutput(
                paper_trading_epoch_id=active_epoch.id,
                prediction_run_id=prediction_run.id,
                candidate_id=candidate.id,
                market_family=candidate.market_family,
                probability_raw=candidate.probability_raw,
                probability_calibrated=candidate.probability_calibrated,
                fair_value=candidate.fair_value,
                executable_price=candidate.executable_price,
                expected_value_gross=candidate.expected_value,
                fee_estimate=candidate.fee_estimate,
                expected_value_net=candidate.net_expected_value,
                probability_edge=candidate.probability_edge,
                executable_price_source=candidate.executable_price_source,
                price_status=candidate.price_status,
                data_quality=candidate.data_quality,
                calibration_status=candidate.calibration_status,
                decision_reason=decision,
                raw_output={
                    **(candidate.scoring_rationale or {}),
                    "target_date": day.isoformat(),
                    "price_context": {
                        "side": contract_side,
                        "market_price": float(price_context.market_price) if price_context.market_price is not None else None,
                        "executable_price": float(price) if price is not None else None,
                        "source": price_context.source,
                        "updated_at": price_context.updated_at.isoformat() if price_context.updated_at else None,
                        "staleness_seconds": price_context.staleness_seconds,
                        "status": price_context.status,
                    },
                    "fee_context": {
                        "fee_estimate": float(fee) if fee is not None else None,
                        "fee_rate": float(settings.kalshi_trade_fee_rate),
                        "fee_estimate_mode": settings.kalshi_fee_estimate_mode,
                        "fee_rounding_mode": settings.kalshi_fee_rounding_mode,
                        "assume_taker": settings.kalshi_assume_taker,
                        "quantity": 1,
                    },
                    "gate_diagnostics": diagnostics,
                    "sweep": sweep_summary,
                },
            )
            session.add(output)
            outputs_by_candidate_id[candidate.id] = output

            if price is not None and (
                decision == "eligible_for_paper_trade" or (dry_run_candidates_only and eligible_for_intent)
            ):
                trade_intents.append(
                    TradeIntent(
                        candidate=candidate,
                        game=game,
                        market=market,
                        price=price,
                        labels=labels,
                        score=_trade_rank_score(candidate),
                    )
                )

    line_classification_counts = _apply_candidate_line_classifications(evaluated_candidates, outputs_by_candidate_id)
    candidate_exposure_field_counts = _candidate_exposure_field_counts(evaluated_candidates)
    candidate_probability_adapter_field_counts = _candidate_probability_adapter_field_counts(evaluated_candidates)
    probability_adapter_summary = _probability_adapter_summary(evaluated_candidates)
    for candidate, open_trade in open_trade_metadata_refreshes:
        _copy_exposure_metadata_to_trade(open_trade, candidate)
        session.add(open_trade)
    for candidate in evaluated_candidates:
        session.add(candidate)
    for output in outputs_by_candidate_id.values():
        session.add(output)

    if dry_run_candidates_only:
        side_guarded_trades = trade_intents
        line_selected_trades = side_guarded_trades
        scope_selected_trades = line_selected_trades
        side_conflict_counts = {"no_trade_conflicting_side_signals": 0}
        line_selection_counts = {
            "line_selection_groups_considered": 0,
            "line_selection_candidates_kept": len(line_selected_trades),
            "line_selection_candidates_rejected": 0,
        }
        game_scope_counts = {
            "game_scope_correlation_groups_considered": 0,
            "game_scope_correlation_candidates_kept": len(scope_selected_trades),
            "game_scope_correlation_candidates_rejected": 0,
            "no_trade_game_scope_correlation_cap": 0,
            "no_trade_same_game_scope_correlation_not_best": 0,
        }
        game_scope_summary = {
            "limit": max(settings.paper_max_trades_per_game_scope, 1),
            "groups": {},
            "dry_run_candidates_only": True,
            "guard_skipped": True,
        }
    else:
        side_guarded_trades, side_conflict_counts = _apply_side_conflict_guard(trade_intents)
        line_selected_trades, line_selection_counts = _apply_line_selection(side_guarded_trades)
        scope_selected_trades, game_scope_counts, game_scope_summary = _apply_game_scope_correlation(
            session,
            line_selected_trades,
            active_epoch.id,
        )
    selector_selected_trades, selector_counts, selector_summary = apply_live_like_selector(
        candidates=evaluated_candidates,
        intents=scope_selected_trades,
        settings=settings,
        dry_run_candidates_only=dry_run_candidates_only,
    )
    candidate_selector_field_counts = _candidate_selector_field_counts(evaluated_candidates)
    for candidate in evaluated_candidates:
        output = outputs_by_candidate_id.get(candidate.id)
        if output is not None:
            output.decision_reason = candidate.decision
            raw = dict(output.raw_output or {})
            raw["selector"] = selector_metadata_payload(candidate)
            raw["gate_diagnostics"] = candidate.gate_diagnostics or {}
            output.raw_output = raw
            session.add(output)
        session.add(candidate)
    for intent in trade_intents:
        output = outputs_by_candidate_id.get(intent.candidate.id)
        if output is not None:
            output.decision_reason = intent.candidate.decision
            raw = dict(output.raw_output or {})
            raw["selector"] = selector_metadata_payload(intent.candidate)
            raw["gate_diagnostics"] = intent.candidate.gate_diagnostics or {}
            output.raw_output = raw
            session.add(output)
        session.add(intent.candidate)

    cap_input_trades = [] if dry_run_candidates_only else selector_selected_trades
    selected_trades, cap_counts, trade_allocation_summary = _apply_trade_caps(
        session,
        cap_input_trades,
        day,
        day_start,
        day_end,
        active_epoch.id,
        enforce_slate_cap=False,
        enforce_sweep_cap=False,
        enforce_time_reserve=False,
        enforce_open_position_cap=False,
        enforce_low_price_new_caps=False,
        enforce_side_new_caps=False,
    )
    session.flush()
    for intent in line_selected_trades:
        output = outputs_by_candidate_id.get(intent.candidate.id)
        if output is not None:
            output.decision_reason = intent.candidate.decision
            raw = dict(output.raw_output or {})
            raw["selector"] = selector_metadata_payload(intent.candidate)
            raw["gate_diagnostics"] = intent.candidate.gate_diagnostics or {}
            output.raw_output = raw
            session.add(output)

    bankroll_for_sizing = Decimal(calculate_paper_portfolio(session, epoch=active_epoch).portfolio_value)
    risk_basis_context = {
        "risk_limit_basis_type": "active_epoch_portfolio_value",
        "risk_limit_basis_amount": float(bankroll_for_sizing),
    }

    sized_selected_trades: list[TradeIntent] = []
    sizing_rejections = 0
    for intent in selected_trades:
        sizing = _sizing_context(
            bankroll=bankroll_for_sizing,
            price=intent.price,
            one_contract_net_ev=intent.candidate.net_expected_value,
        )
        if sizing.contracts < settings.paper_min_contracts:
            intent.candidate.decision = "no_trade_insufficient_bankroll_or_contract_size"
            _update_gate(intent.candidate, "gate_caps_ok", False)
            intent.candidate.scoring_rationale = {
                **(intent.candidate.scoring_rationale or {}),
                "risk_limit_basis": risk_basis_context,
            }
            intent.candidate.bankroll_at_entry = sizing.bankroll_at_entry
            intent.candidate.risk_pct = sizing.risk_pct
            intent.candidate.risk_dollars = sizing.risk_dollars
            intent.candidate.contracts = sizing.contracts
            intent.candidate.estimated_cost_per_contract = sizing.estimated_cost_per_contract
            intent.candidate.estimated_total_cost = sizing.estimated_total_cost
            intent.candidate.one_contract_expected_value = sizing.one_contract_expected_value
            intent.candidate.sized_expected_value = sizing.sized_expected_value
            intent.candidate.one_contract_fee_estimate = sizing.one_contract_fee_estimate
            intent.candidate.total_fee_estimate = sizing.total_fee_estimate
            output = outputs_by_candidate_id.get(intent.candidate.id)
            if output is not None:
                output.decision_reason = intent.candidate.decision
                raw = dict(output.raw_output or {})
                raw["sizing"] = {**sizing.as_dict(), **risk_basis_context}
                raw["gate_diagnostics"] = intent.candidate.gate_diagnostics or {}
                output.raw_output = raw
                session.add(output)
            session.add(intent.candidate)
            sizing_rejections += 1
            continue
        intent.quantity = sizing.contracts
        intent.sizing = {**sizing.as_dict(), **risk_basis_context, "original_contracts": sizing.contracts}
        intent.candidate.scoring_rationale = {
            **(intent.candidate.scoring_rationale or {}),
            "risk_limit_basis": risk_basis_context,
        }
        intent.candidate.bankroll_at_entry = sizing.bankroll_at_entry
        intent.candidate.risk_pct = sizing.risk_pct
        intent.candidate.risk_dollars = sizing.risk_dollars
        intent.candidate.contracts = sizing.contracts
        intent.candidate.estimated_cost_per_contract = sizing.estimated_cost_per_contract
        intent.candidate.estimated_total_cost = sizing.estimated_total_cost
        intent.candidate.one_contract_expected_value = sizing.one_contract_expected_value
        intent.candidate.sized_expected_value = sizing.sized_expected_value
        intent.candidate.one_contract_fee_estimate = sizing.one_contract_fee_estimate
        intent.candidate.total_fee_estimate = sizing.total_fee_estimate
        sized_selected_trades.append(intent)

    max_early_new_trades = None
    if trade_allocation_summary["early_window"]:
        max_early_new_trades = max(
            int(trade_allocation_summary["early_window_allowed"] or 0)
            - int(trade_allocation_summary["existing_slate_trades"]),
            0,
        )
    risk_selected_trades, risk_cap_counts, risk_cap_summary = _apply_aggregate_risk_caps(
        session,
        sized_selected_trades,
        target_date=day,
        day_start=day_start,
        day_end=day_end,
        epoch_id=active_epoch.id,
        bankroll=bankroll_for_sizing,
        existing_slate_trades=int(trade_allocation_summary["existing_slate_trades"]),
        max_slate_trades=settings.paper_max_trades_per_slate,
        existing_open_positions=int(trade_allocation_summary["existing_open_trades"]),
        max_open_positions=settings.paper_max_open_positions,
        max_new_trades=settings.paper_max_new_trades_per_sweep,
        max_early_new_trades=max_early_new_trades,
        existing_low_price_trades=int(trade_allocation_summary["low_price_existing_slate"]),
        max_low_price_trades_per_slate=settings.paper_low_price_max_trades_per_slate,
        max_low_price_trades_per_sweep=settings.paper_low_price_max_trades_per_sweep,
        existing_side_trades=dict(trade_allocation_summary["side_existing_slate"]),
        max_same_side_trades_per_slate=settings.paper_max_same_side_trades_per_slate,
    )
    risk_selected_trades, post_cap_size_counts = _apply_post_cap_size_guard(session, risk_selected_trades)
    trade_allocation_summary = {
        **trade_allocation_summary,
        "pre_sizing_candidates_after_preliminary_caps": trade_allocation_summary["new_trades_this_sweep"],
        "pre_sizing_low_price_candidates_after_preliminary_caps": trade_allocation_summary["low_price_new_this_sweep"],
        "new_trades_this_sweep": len(risk_selected_trades),
        "slate_trades_after_sizing_and_risk": int(trade_allocation_summary["existing_slate_trades"])
        + len(risk_selected_trades),
        "open_positions_after_sizing_and_risk": int(trade_allocation_summary["existing_open_trades"])
        + len(risk_selected_trades),
        "early_window_used": int(trade_allocation_summary["existing_slate_trades"]) + len(risk_selected_trades)
        if trade_allocation_summary["early_window"]
        else None,
        "low_price_new_this_sweep": risk_cap_summary["low_price_new_after_sizing_and_risk"],
        "side_new_this_sweep": risk_cap_summary["side_new_after_sizing_and_risk"],
    }
    for intent in sized_selected_trades:
        output = outputs_by_candidate_id.get(intent.candidate.id)
        if output is not None:
            output.decision_reason = intent.candidate.decision
            raw = dict(output.raw_output or {})
            raw["sizing"] = {
                **(intent.sizing or {}),
                "contracts": intent.quantity,
            }
            raw["gate_diagnostics"] = intent.candidate.gate_diagnostics or {}
            output.raw_output = raw
            session.add(output)
        session.add(intent.candidate)

    paper_trade_side_counts = {"yes": 0, "no": 0}
    for rank, intent in enumerate(risk_selected_trades, start=1):
        candidate = intent.candidate
        existing_trade = session.scalar(
            select(PaperTrade)
            .where(PaperTrade.candidate_id == candidate.id)
            .where(PaperTrade.paper_trading_epoch_id == active_epoch.id)
        )
        if existing_trade is not None:
            candidate.decision = "candidate_only_existing_trade"
            _update_gate(candidate, "gate_open_position_ok", False)
            session.add(candidate)
            output = outputs_by_candidate_id.get(candidate.id)
            if output is not None:
                output.decision_reason = candidate.decision
                session.add(output)
            continue
        trade = PaperTrade(
            paper_trading_epoch_id=active_epoch.id,
            candidate_id=candidate.id,
            market_ticker=intent.market.ticker,
            contract_side=candidate.contract_side or "yes",
            entry_price=intent.price,
            current_price=intent.price,
            quantity=intent.quantity,
            entry_time=now,
            status="open",
            expected_value=candidate.sized_expected_value or candidate.net_expected_value,
            market_display=intent.labels.actual_contract_display or intent.labels.market_display,
            selection_display=intent.labels.selection_display,
            matchup_display=intent.labels.matchup_display,
            contract_display=intent.labels.actual_contract_display or intent.labels.contract_display,
            market_family=candidate.market_family,
            line_value=candidate.line_value,
            selection_code=candidate.selection_code,
            over_under_side=candidate.over_under_side,
            inning_scope=candidate.inning_scope,
            settlement_rule_status=candidate.settlement_rule_status,
            training_eligible=candidate.training_eligible,
            bankroll_at_entry=candidate.bankroll_at_entry,
            risk_pct=candidate.risk_pct,
            risk_dollars=candidate.risk_dollars,
            estimated_cost_per_contract=candidate.estimated_cost_per_contract,
            estimated_total_cost=candidate.estimated_total_cost,
            one_contract_expected_value=candidate.one_contract_expected_value,
            sized_expected_value=candidate.sized_expected_value,
            one_contract_fee_estimate=candidate.one_contract_fee_estimate,
            total_fee_estimate=candidate.total_fee_estimate,
        )
        _copy_exposure_metadata_to_trade(trade, candidate)
        _copy_selector_metadata_to_trade(trade, candidate)
        session.add(trade)
        output = outputs_by_candidate_id.get(candidate.id)
        if output is not None:
            output.trade_rank = rank
            output.decision_reason = "paper_trade"
            raw = dict(output.raw_output or {})
            raw["sizing"] = {
                **(intent.sizing or {}),
                "contracts": intent.quantity,
            }
            raw["gate_diagnostics"] = candidate.gate_diagnostics or {}
            output.raw_output = raw
            session.add(output)
        paper_trades += 1
        side_key = (candidate.contract_side or "unknown").lower()
        if side_key in paper_trade_side_counts:
            paper_trade_side_counts[side_key] += 1

    session.flush()
    decision_counts: dict[str, int] = {}
    decision_counts_by_side: dict[str, dict[str, int]] = {}
    edge_values_by_side: dict[str, list[Decimal]] = {"yes": [], "no": []}
    net_ev_values_by_side: dict[str, list[Decimal]] = {"yes": [], "no": []}
    for candidate in evaluated_candidates:
        decision = candidate.decision or "unknown"
        decision_counts[decision] = decision_counts.get(decision, 0) + 1
        side = (candidate.contract_side or "unknown").lower()
        side_bucket = decision_counts_by_side.setdefault(side, {})
        side_bucket[decision] = side_bucket.get(decision, 0) + 1
        if side in edge_values_by_side and candidate.probability_edge is not None:
            edge_values_by_side[side].append(candidate.probability_edge)
        if side in net_ev_values_by_side and candidate.net_expected_value is not None:
            net_ev_values_by_side[side].append(candidate.net_expected_value)
    decision_breakdown_by_family, decision_breakdown_by_scope = _decision_breakdowns(evaluated_candidates)
    gate_summary = _gate_summary(evaluated_candidates)
    opportunity_diagnostics = _opportunity_diagnostics(evaluated_candidates)
    edge_values = [candidate.probability_edge for candidate in evaluated_candidates if candidate.probability_edge is not None]
    net_ev_values = [
        candidate.net_expected_value for candidate in evaluated_candidates if candidate.net_expected_value is not None
    ]
    fee_values = [candidate.fee_estimate for candidate in evaluated_candidates if candidate.fee_estimate is not None]
    edge_or_fee_reasons = {
        "no_trade_edge_too_low",
        "no_trade_fee_adjusted_ev_too_low",
        "no_trade_probability_edge_low",
        "no_trade_missing_fee_estimate",
        "no_trade_low_price_ev_too_low",
        "no_trade_low_price_probability_edge_low",
    }
    trades_blocked_by_edge_or_fee = sum(decision_counts.get(reason, 0) for reason in edge_or_fee_reasons)
    all_cap_counts = {**cap_counts}
    for key, value in selector_counts.items():
        all_cap_counts[key] = all_cap_counts.get(key, 0) + value
    for key, value in side_conflict_counts.items():
        all_cap_counts[key] = all_cap_counts.get(key, 0) + value
    for key, value in game_scope_counts.items():
        all_cap_counts[key] = all_cap_counts.get(key, 0) + value
    for key, value in risk_cap_counts.items():
        all_cap_counts[key] = all_cap_counts.get(key, 0) + value
    for key, value in post_cap_size_counts.items():
        all_cap_counts[key] = all_cap_counts.get(key, 0) + value
    trades_blocked_by_caps = sum(
        value for key, value in all_cap_counts.items() if key != "aggregate_risk_quantity_reduced"
    ) + sizing_rejections
    low_price_controls = _low_price_control_summary(evaluated_candidates, decision_counts)

    sweep_result = {
        **sweep_summary,
        "candidates_in_window": created_or_updated,
        "paper_trades_in_window": paper_trades,
        "mappings_considered": len(source_mappings),
        "mappings_in_window": len(mappings),
    }
    result_status = (
        "skipped_no_games_in_window"
        if sweep_window_enabled and int(sweep_result["games_in_window"]) == 0
        else "completed"
    )

    snapshot = None if dry_run_candidates_only else create_balance_snapshot(session, source="candidate_engine")
    prediction_run.completed_at = now
    prediction_run.status = "completed"
    prediction_run.candidates_evaluated = created_or_updated
    prediction_run.trades_created = paper_trades
    prediction_run.summary = {
        "status": result_status,
        **sweep_result,
        "candidate_sweep_window": sweep_result,
        "decision_counts": decision_counts,
        "decision_counts_by_side": decision_counts_by_side,
        "candidate_diagnostics": gate_summary,
        "quality_ev_diagnostics": opportunity_diagnostics,
        "by_family": decision_breakdown_by_family,
        "by_scope": decision_breakdown_by_scope,
        "cap_counts": all_cap_counts,
        "risk_caps": risk_cap_summary,
        "trade_allocation": trade_allocation_summary,
        "low_price_controls": low_price_controls,
        "game_scope_correlation": game_scope_summary,
        "spread_trading_enabled": settings.paper_spread_trading_enabled,
        "paper_spread_trading_enabled": settings.paper_spread_trading_enabled,
        "paper_first_five_spread_trading_enabled": settings.paper_spread_trading_enabled,
        "paper_full_game_spread_trading_enabled": settings.paper_full_game_spread_trading_enabled,
        "full_game_spread_audit_gate_enabled": True,
        "full_game_spread_requires_trusted_audit": True,
        "full_game_spread_audit_only": not settings.paper_full_game_spread_trading_enabled,
        "side_aware_candidates_enabled": True,
        "risk_caps_enabled": True,
        "candidates_yes": len([candidate for candidate in evaluated_candidates if candidate.contract_side == "yes"]),
        "candidates_no": len([candidate for candidate in evaluated_candidates if candidate.contract_side == "no"]),
        "paper_trades_yes": paper_trade_side_counts["yes"],
        "paper_trades_no": paper_trade_side_counts["no"],
        "avg_net_ev_by_side": {
            "yes": _avg_decimal(net_ev_values_by_side["yes"]),
            "no": _avg_decimal(net_ev_values_by_side["no"]),
        },
        "avg_probability_edge_by_side": {
            "yes": _avg_decimal(edge_values_by_side["yes"]),
            "no": _avg_decimal(edge_values_by_side["no"]),
        },
        "actual_contract_parse_failures": 0,
        "side_aware_verified_count": created_or_updated,
        "exposure_taxonomy_version": EXPOSURE_TAXONOMY_VERSION,
        "line_classification_policy_version": LINE_CLASSIFICATION_POLICY_VERSION,
        "probability_adapter_policy_version": PROBABILITY_ADAPTER_POLICY_VERSION,
        "candidate_exposure_field_counts": candidate_exposure_field_counts,
        "candidate_probability_adapter_field_counts": candidate_probability_adapter_field_counts,
        **probability_adapter_summary,
        "candidate_selector_field_counts": candidate_selector_field_counts,
        "line_classification_counts": line_classification_counts,
        "line_selection": line_selection_counts,
        "selector": selector_summary,
        **selector_summary,
        "warnings": warnings,
        "eligible_trade_intents": len(trade_intents),
        "trade_eligible_after_side_conflict_guard": len(side_guarded_trades),
        "trade_eligible_after_line_selection": len(line_selected_trades),
        "trade_eligible_after_game_scope_correlation": len(scope_selected_trades),
        "trade_eligible_after_live_like_selector": len(selector_selected_trades),
        "trade_eligible_before_caps": len(selector_selected_trades),
        "trades_blocked_by_edge_or_fee": trades_blocked_by_edge_or_fee,
        "trades_blocked_by_line_selection": line_selection_counts["line_selection_candidates_rejected"],
        "trades_blocked_by_live_like_selector": sum(selector_counts.values()),
        "trades_blocked_by_game_scope_correlation": game_scope_counts["game_scope_correlation_candidates_rejected"],
        "stale_price_count": stale_price_count,
        "paper_trades": paper_trades,
        "sizing_rejections": sizing_rejections,
    }
    session.add(prediction_run)
    session.commit()
    zero_trade_reason = (
        "skipped_no_games_in_window"
        if result_status == "skipped_no_games_in_window"
        else "no_candidates_missing_mappings"
        if not mappings and paper_trades == 0
        else _zero_trade_reason(decision_counts)
        if paper_trades == 0
        else None
    )
    return {
        "status": result_status,
        "date": int(day.strftime("%Y%m%d")),
        "target_date": day.isoformat(),
        "current_eastern_date": _eastern_date(now).isoformat(),
        **sweep_result,
        "candidates": created_or_updated,
        "candidates_evaluated": created_or_updated,
        "candidate_only_count": sum(count for reason, count in decision_counts.items() if reason.startswith("candidate_only")),
        "evaluated_game_count": len({game.id for _mapping, game, _market in mappings}),
        "mappings_considered": len(source_mappings),
        "mappings_evaluated": len(mappings),
        "paper_trades": paper_trades,
        "trades_created": paper_trades,
        "model_version": model_version.version_tag,
        "parameter_version": parameter_version.version_tag,
        "feature_version": FEATURE_VERSION,
        "prediction_run_id": prediction_run.id,
        "prediction_run_target_date": prediction_run.target_date.isoformat() if prediction_run.target_date else None,
        "snapshot_id": snapshot.id if snapshot is not None else None,
        "decision_counts": decision_counts,
        "decision_counts_by_side": decision_counts_by_side,
        "candidate_diagnostics": gate_summary,
        "quality_ev_diagnostics": opportunity_diagnostics,
        "decision_breakdown_by_family": decision_breakdown_by_family,
        "decision_breakdown_by_scope": decision_breakdown_by_scope,
        "cap_counts": all_cap_counts,
        "risk_caps": risk_cap_summary,
        "trade_allocation": trade_allocation_summary,
        "low_price_controls": low_price_controls,
        "game_scope_correlation": game_scope_summary,
        "spread_trading_enabled": settings.paper_spread_trading_enabled,
        "paper_spread_trading_enabled": settings.paper_spread_trading_enabled,
        "paper_first_five_spread_trading_enabled": settings.paper_spread_trading_enabled,
        "paper_full_game_spread_trading_enabled": settings.paper_full_game_spread_trading_enabled,
        "full_game_spread_audit_gate_enabled": True,
        "full_game_spread_requires_trusted_audit": True,
        "full_game_spread_audit_only": not settings.paper_full_game_spread_trading_enabled,
        "side_aware_candidates_enabled": True,
        "risk_caps_enabled": True,
        "candidates_yes": len([candidate for candidate in evaluated_candidates if candidate.contract_side == "yes"]),
        "candidates_no": len([candidate for candidate in evaluated_candidates if candidate.contract_side == "no"]),
        "paper_trades_yes": paper_trade_side_counts["yes"],
        "paper_trades_no": paper_trade_side_counts["no"],
        "avg_net_ev_by_side": {
            "yes": _avg_decimal(net_ev_values_by_side["yes"]),
            "no": _avg_decimal(net_ev_values_by_side["no"]),
        },
        "avg_probability_edge_by_side": {
            "yes": _avg_decimal(edge_values_by_side["yes"]),
            "no": _avg_decimal(edge_values_by_side["no"]),
        },
        "actual_contract_parse_failures": 0,
        "side_aware_verified_count": created_or_updated,
        "exposure_taxonomy_version": EXPOSURE_TAXONOMY_VERSION,
        "line_classification_policy_version": LINE_CLASSIFICATION_POLICY_VERSION,
        "probability_adapter_policy_version": PROBABILITY_ADAPTER_POLICY_VERSION,
        "candidate_exposure_field_counts": candidate_exposure_field_counts,
        "candidate_probability_adapter_field_counts": candidate_probability_adapter_field_counts,
        **probability_adapter_summary,
        "candidate_selector_field_counts": candidate_selector_field_counts,
        "line_classification_counts": line_classification_counts,
        "selector": selector_summary,
        **selector_summary,
        "trade_eligible_before_quality": gate_summary["trade_eligible_before_quality"],
        "trade_eligible_after_quality": gate_summary["trade_eligible_after_quality"],
        "blocked_by_quality_only": gate_summary["blocked_by_quality_only"],
        "would_pass_ev_if_quality_allowed": gate_summary["would_pass_ev_if_quality_allowed"],
        "would_pass_edge_if_quality_allowed": gate_summary["would_pass_edge_if_quality_allowed"],
        "ev_edge_pass_but_quality_fail": gate_summary["ev_edge_pass_but_quality_fail"],
        "blocked_by_ev": gate_summary["blocked_by_ev"],
        "blocked_by_edge": gate_summary["blocked_by_edge"],
        "blocked_by_price": gate_summary["blocked_by_price"],
        "blocked_by_mapping": gate_summary["blocked_by_mapping"],
        "blocked_by_push": gate_summary["blocked_by_push"],
        "blocked_by_line_selection": gate_summary["blocked_by_line_selection"],
        "blocked_by_caps": gate_summary["blocked_by_caps"],
        "trade_eligible_after_side_conflict_guard": len(side_guarded_trades),
        "trade_eligible_before_caps": len(selector_selected_trades),
        "trade_eligible_after_ev_filters": len(trade_intents),
        "trade_eligible_after_line_selection": len(line_selected_trades),
        "trade_eligible_after_game_scope_correlation": len(scope_selected_trades),
        "trade_eligible_after_live_like_selector": len(selector_selected_trades),
        "trades_blocked_by_caps": trades_blocked_by_caps,
        "trades_blocked_by_edge_or_fee": trades_blocked_by_edge_or_fee,
        "trades_blocked_by_line_selection": line_selection_counts["line_selection_candidates_rejected"],
        "trades_blocked_by_line_selection_or_correlation": line_selection_counts["line_selection_candidates_rejected"]
        + all_cap_counts.get("no_trade_correlated_market_cap", 0)
        + game_scope_counts["game_scope_correlation_candidates_rejected"],
        "trades_blocked_by_live_like_selector": sum(selector_counts.values()),
        "trades_blocked_by_game_scope_correlation": game_scope_counts["game_scope_correlation_candidates_rejected"],
        "stale_price_count": stale_price_count,
        "non_executable_price_count": non_executable_price_count,
        "line_selection_groups_considered": line_selection_counts["line_selection_groups_considered"],
        "line_selection_candidates_kept": line_selection_counts["line_selection_candidates_kept"],
        "line_selection_candidates_rejected": line_selection_counts["line_selection_candidates_rejected"],
        "game_scope_correlation_groups_considered": game_scope_counts["game_scope_correlation_groups_considered"],
        "game_scope_correlation_candidates_kept": game_scope_counts["game_scope_correlation_candidates_kept"],
        "game_scope_correlation_candidates_rejected": game_scope_counts[
            "game_scope_correlation_candidates_rejected"
        ],
        "avg_probability_edge": _avg_decimal(edge_values),
        "avg_expected_value_net": _avg_decimal(net_ev_values),
        "max_expected_value_net": _max_decimal(net_ev_values),
        "average_data_quality": gate_summary["average_data_quality"],
        "min_data_quality": gate_summary["min_data_quality"],
        "max_data_quality": gate_summary["max_data_quality"],
        "raw_feature_snapshot_data_quality_avg": gate_summary["raw_feature_snapshot_data_quality_avg"],
        "raw_feature_snapshot_data_quality_max": gate_summary["raw_feature_snapshot_data_quality_max"],
        "paper_observation_data_quality_avg": gate_summary["paper_observation_data_quality_avg"],
        "paper_observation_data_quality_max": gate_summary["paper_observation_data_quality_max"],
        "quality_threshold": gate_summary["quality_threshold"],
        "candidate_stage_market_context_status_counts": gate_summary[
            "candidate_stage_market_context_status_counts"
        ],
        "quality_block_reason_counts": gate_summary["quality_block_reason_counts"],
        "top_quality_blockers": gate_summary["top_quality_blockers"],
        "ev_pass_count": opportunity_diagnostics["ev_pass_count"],
        "edge_pass_count": opportunity_diagnostics["edge_pass_count"],
        "ev_and_edge_pass_count": opportunity_diagnostics["ev_and_edge_pass_count"],
        "deduped_ev_edge_pass_count_by_game_scope_family": opportunity_diagnostics[
            "deduped_ev_edge_pass_count_by_game_scope_family"
        ],
        "deduped_pre_quality_trade_eligible_count_by_game_scope_family": opportunity_diagnostics[
            "deduped_pre_quality_trade_eligible_count_by_game_scope_family"
        ],
        "top_counterfactual_candidates_blocked_by_quality": opportunity_diagnostics[
            "top_counterfactual_candidates_blocked_by_quality"
        ],
        "average_net_ev_among_ev_passing": gate_summary["average_net_ev_among_ev_passing"],
        "average_probability_edge_among_edge_passing": gate_summary["average_probability_edge_among_edge_passing"],
        "fee_estimate_avg": _avg_decimal(fee_values),
        "zero_trade_reason": zero_trade_reason,
        "warnings": warnings,
        "trade_policy": prediction_run.trade_policy,
        "active_epoch": {
            "id": active_epoch.id,
            "epoch_key": active_epoch.epoch_key,
            "display_name": active_epoch.display_name,
            "starting_balance": float(active_epoch.starting_balance),
        },
    }
