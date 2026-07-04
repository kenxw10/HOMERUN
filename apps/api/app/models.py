from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, JSON, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func, text

from app.database import Base


DECISION_REASON_MAX_LENGTH = 120


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class BotSetting(TimestampMixin, Base):
    __tablename__ = "bot_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    value: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    description: Mapped[str | None] = mapped_column(Text)


class PaperTradingEpoch(TimestampMixin, Base):
    __tablename__ = "paper_trading_epochs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    epoch_key: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="active", nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(40), default="paper", nullable=False)
    starting_balance: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    current_balance_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("balance_snapshots.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archive_reason: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[dict[str, object] | None] = mapped_column(JSON)


class BalanceSnapshot(TimestampMixin, Base):
    __tablename__ = "balance_snapshots"
    __table_args__ = (Index("ix_balance_snapshots_epoch_captured", "paper_trading_epoch_id", "captured_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    paper_trading_epoch_id: Mapped[int | None] = mapped_column(ForeignKey("paper_trading_epochs.id"), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cash_balance: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    portfolio_value: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    source: Mapped[str] = mapped_column(String(40), default="paper", nullable=False)
    snapshot_type: Mapped[str | None] = mapped_column(String(40))


class MlbGame(TimestampMixin, Base):
    __tablename__ = "mlb_games"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_game_id: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    home_team: Mapped[str] = mapped_column(String(120), nullable=False)
    away_team: Mapped[str] = mapped_column(String(120), nullable=False)
    home_abbreviation: Mapped[str | None] = mapped_column(String(12))
    away_abbreviation: Mapped[str | None] = mapped_column(String(12))
    scheduled_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="scheduled", nullable=False)
    home_score: Mapped[int | None] = mapped_column(Integer)
    away_score: Mapped[int | None] = mapped_column(Integer)
    raw_payload: Mapped[dict[str, object] | None] = mapped_column(JSON)


class KalshiMarket(TimestampMixin, Base):
    __tablename__ = "kalshi_markets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kalshi_market_id: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    ticker: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    event_ticker: Mapped[str | None] = mapped_column(String(120))
    title: Mapped[str] = mapped_column(Text, nullable=False)
    subtitle: Mapped[str | None] = mapped_column(Text)
    rules: Mapped[str | None] = mapped_column(Text)
    yes_subtitle: Mapped[str | None] = mapped_column(Text)
    no_subtitle: Mapped[str | None] = mapped_column(Text)
    yes_bid: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    yes_ask: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    yes_mid: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    no_bid: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    no_ask: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    no_mid: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    last_price: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    best_yes_bid: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    best_no_bid: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    implied_yes_ask: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    implied_no_ask: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    market_price_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    websocket_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    market_data_source: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(40), default="untracked", nullable=False)
    raw_status: Mapped[str | None] = mapped_column(String(40))
    open_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    close_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    occurrence_datetime: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolve_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    market_family: Mapped[str | None] = mapped_column(String(80))
    market_type: Mapped[str | None] = mapped_column(String(80))
    line_value: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    selection_code: Mapped[str | None] = mapped_column(String(40))
    over_under_side: Mapped[str | None] = mapped_column(String(20))
    inning_scope: Mapped[str | None] = mapped_column(String(40))
    settlement_rule_status: Mapped[str | None] = mapped_column(String(80))
    raw_payload: Mapped[dict[str, object] | None] = mapped_column(JSON)
    orderbook_raw: Mapped[dict[str, object] | None] = mapped_column(JSON)


class MarketMapping(TimestampMixin, Base):
    __tablename__ = "market_mappings"
    __table_args__ = (UniqueConstraint("mlb_game_id", "kalshi_market_id", name="uq_game_market"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mlb_game_id: Mapped[int] = mapped_column(ForeignKey("mlb_games.id"), nullable=False)
    kalshi_market_id: Mapped[int] = mapped_column(ForeignKey("kalshi_markets.id"), nullable=False)
    mapping_status: Mapped[str] = mapped_column(String(40), default="candidate", nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    rationale: Mapped[str | None] = mapped_column(Text)
    resolver_strategy: Mapped[str | None] = mapped_column(String(80))
    validation_status: Mapped[str | None] = mapped_column(String(80))
    market_family: Mapped[str | None] = mapped_column(String(80))
    market_type: Mapped[str | None] = mapped_column(String(80))
    line_value: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    selection_code: Mapped[str | None] = mapped_column(String(40))
    over_under_side: Mapped[str | None] = mapped_column(String(20))
    inning_scope: Mapped[str | None] = mapped_column(String(40))
    settlement_rule_status: Mapped[str | None] = mapped_column(String(80))
    mapping_metadata: Mapped[dict[str, object] | None] = mapped_column(JSON)


class ModelVersion(TimestampMixin, Base):
    __tablename__ = "model_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version_tag: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    trained_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metrics: Mapped[dict[str, object] | None] = mapped_column(JSON)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    model_family: Mapped[str | None] = mapped_column(String(80))
    feature_version: Mapped[str | None] = mapped_column(String(80))
    role: Mapped[str | None] = mapped_column(String(40))
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ModelCandidate(TimestampMixin, Base):
    __tablename__ = "model_candidates"
    __table_args__ = (
        Index(
            "ix_model_candidates_epoch_governance_counts",
            "paper_trading_epoch_id",
            "mlb_game_id",
            "training_eligible",
            "feature_version",
            "outcome",
            "price_status",
            "market_family",
            "target_date",
            "evaluated_at",
        ),
        Index(
            "ix_model_candidates_epoch_decision_scope",
            "paper_trading_epoch_id",
            "evaluated_at",
            "market_family",
            "market_type",
            "inning_scope",
            "decision",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    paper_trading_epoch_id: Mapped[int | None] = mapped_column(ForeignKey("paper_trading_epochs.id"), index=True)
    mlb_game_id: Mapped[int | None] = mapped_column(ForeignKey("mlb_games.id"))
    kalshi_market_id: Mapped[int | None] = mapped_column(ForeignKey("kalshi_markets.id"))
    mapping_id: Mapped[int | None] = mapped_column(ForeignKey("market_mappings.id"))
    model_version_id: Mapped[int | None] = mapped_column(ForeignKey("model_versions.id"))
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    features: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    probability: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))
    model_probability: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))
    probability_raw: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))
    probability_calibrated: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))
    fair_value: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    market_price: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    executable_price: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    expected_value: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    fee_estimate: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    net_expected_value: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    probability_edge: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    target_date: Mapped[date | None] = mapped_column(Date)
    executable_price_source: Mapped[str | None] = mapped_column(String(80))
    market_price_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    price_staleness_seconds: Mapped[int | None] = mapped_column(Integer)
    price_status: Mapped[str | None] = mapped_column(String(80))
    market_type: Mapped[str | None] = mapped_column(String(80))
    time_bucket: Mapped[str | None] = mapped_column(String(40))
    time_to_start_minutes: Mapped[int | None] = mapped_column(Integer)
    contract_side: Mapped[str | None] = mapped_column(String(10))
    decision: Mapped[str] = mapped_column(String(DECISION_REASON_MAX_LENGTH), default="no_trade", nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(40))
    outcome_source: Mapped[str | None] = mapped_column(String(80))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    model_version_tag: Mapped[str | None] = mapped_column(String(120))
    scoring_rationale: Mapped[dict[str, object] | None] = mapped_column(JSON)
    market_display: Mapped[str | None] = mapped_column(Text)
    selection_display: Mapped[str | None] = mapped_column(String(40))
    matchup_display: Mapped[str | None] = mapped_column(String(80))
    contract_display: Mapped[str | None] = mapped_column(Text)
    feature_version: Mapped[str | None] = mapped_column(String(80))
    training_eligible: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    training_exclusion_reason: Mapped[str | None] = mapped_column(String(120))
    data_quality: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    calibration_status: Mapped[str | None] = mapped_column(String(80))
    market_family: Mapped[str | None] = mapped_column(String(80))
    line_value: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    selection_code: Mapped[str | None] = mapped_column(String(40))
    over_under_side: Mapped[str | None] = mapped_column(String(20))
    inning_scope: Mapped[str | None] = mapped_column(String(40))
    settlement_rule_status: Mapped[str | None] = mapped_column(String(80))
    economic_exposure_label: Mapped[str | None] = mapped_column(Text)
    economic_exposure_key: Mapped[str | None] = mapped_column(String(180))
    economic_exposure_family: Mapped[str | None] = mapped_column(String(40))
    economic_exposure_scope: Mapped[str | None] = mapped_column(String(40))
    economic_exposure_direction: Mapped[str | None] = mapped_column(String(40))
    economic_exposure_team: Mapped[str | None] = mapped_column(String(40))
    economic_exposure_line: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    contract_mechanics_label: Mapped[str | None] = mapped_column(Text)
    concept_cluster_key: Mapped[str | None] = mapped_column(String(160))
    same_game_concept_cluster_key: Mapped[str | None] = mapped_column(String(180))
    line_class: Mapped[str | None] = mapped_column(String(40))
    line_class_reason: Mapped[str | None] = mapped_column(String(120))
    line_ladder_rank: Mapped[int | None] = mapped_column(Integer)
    line_ladder_distance_from_central: Mapped[int | None] = mapped_column(Integer)
    line_ladder_size: Mapped[int | None] = mapped_column(Integer)
    exposure_taxonomy_version: Mapped[str | None] = mapped_column(String(80))
    line_classification_policy_version: Mapped[str | None] = mapped_column(String(80))
    selector_policy_version: Mapped[str | None] = mapped_column(String(80))
    selector_mode: Mapped[str | None] = mapped_column(String(40))
    selector_status: Mapped[str | None] = mapped_column(String(40))
    selector_decision: Mapped[str | None] = mapped_column(String(120))
    selector_rejection_reason: Mapped[str | None] = mapped_column(String(120))
    selector_threshold_profile: Mapped[str | None] = mapped_column(String(120))
    selector_min_net_ev: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    selector_min_prob_edge: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    selector_min_data_quality: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    selector_line_class_policy: Mapped[str | None] = mapped_column(String(120))
    selector_concept_cluster_key: Mapped[str | None] = mapped_column(String(160))
    selector_same_game_concept_cluster_key: Mapped[str | None] = mapped_column(String(180))
    selector_cluster_rank: Mapped[int | None] = mapped_column(Integer)
    selector_cluster_rank_score: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    selector_selected_from_cluster: Mapped[bool | None] = mapped_column(Boolean)
    selector_shadow_only: Mapped[bool | None] = mapped_column(Boolean)
    selector_live_like_eligible_before_cluster: Mapped[bool | None] = mapped_column(Boolean)
    selector_live_like_eligible_after_cluster: Mapped[bool | None] = mapped_column(Boolean)
    probability_adapter_key: Mapped[str | None] = mapped_column(String(120))
    probability_adapter_version: Mapped[str | None] = mapped_column(String(120))
    probability_adapter_policy_version: Mapped[str | None] = mapped_column(String(120))
    probability_adapter_family: Mapped[str | None] = mapped_column(String(80))
    probability_adapter_scope: Mapped[str | None] = mapped_column(String(40))
    probability_adapter_rationale: Mapped[str | None] = mapped_column(Text)
    probability_adapter_calibration_hook: Mapped[str | None] = mapped_column(String(120))
    probability_adapter_calibration_version: Mapped[str | None] = mapped_column(String(120))
    probability_adapter_feature_policy_version: Mapped[str | None] = mapped_column(String(120))
    probability_adapter_metadata: Mapped[dict[str, object] | None] = mapped_column(JSON)
    probability_hardening_policy_version: Mapped[str | None] = mapped_column(String(120))
    probability_hardening_enabled: Mapped[bool | None] = mapped_column(Boolean)
    probability_raw_adapter: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))
    probability_before_hardening: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))
    probability_after_hardening: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))
    probability_hardening_delta: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    probability_hardening_applied: Mapped[bool | None] = mapped_column(Boolean)
    probability_hardening_reason: Mapped[str | None] = mapped_column(String(160))
    probability_hardening_status: Mapped[str | None] = mapped_column(String(80))
    probability_hardening_line_class: Mapped[str | None] = mapped_column(String(40))
    probability_hardening_line_class_policy: Mapped[str | None] = mapped_column(String(120))
    probability_hardening_consistency_status: Mapped[str | None] = mapped_column(String(80))
    probability_hardening_monotonicity_status: Mapped[str | None] = mapped_column(String(80))
    probability_hardening_ladder_role: Mapped[str | None] = mapped_column(String(80))
    probability_hardening_ladder_size: Mapped[int | None] = mapped_column(Integer)
    probability_hardening_ladder_rank: Mapped[int | None] = mapped_column(Integer)
    probability_hardening_distance_from_central: Mapped[int | None] = mapped_column(Integer)
    probability_hardening_central_reference_line: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    probability_hardening_central_reference_probability: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))
    probability_hardening_dampening_factor: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    probability_hardening_shadow_only: Mapped[bool | None] = mapped_column(Boolean)
    probability_hardening_block_recommendation: Mapped[bool | None] = mapped_column(Boolean)
    probability_hardening_error_reason: Mapped[str | None] = mapped_column(String(160))
    risk_governance_policy_version: Mapped[str | None] = mapped_column(String(120))
    risk_governance_enabled: Mapped[bool | None] = mapped_column(Boolean)
    risk_governance_status: Mapped[str | None] = mapped_column(String(80))
    risk_governance_decision: Mapped[str | None] = mapped_column(String(120))
    risk_governance_rejection_reason: Mapped[str | None] = mapped_column(String(160))
    risk_governance_family_status: Mapped[str | None] = mapped_column(String(80))
    risk_governance_family_cap_status: Mapped[str | None] = mapped_column(String(80))
    risk_governance_concept_cluster_cap_status: Mapped[str | None] = mapped_column(String(80))
    risk_governance_same_game_cap_status: Mapped[str | None] = mapped_column(String(80))
    risk_governance_alternate_line_cap_status: Mapped[str | None] = mapped_column(String(80))
    risk_governance_low_price_tail_cap_status: Mapped[str | None] = mapped_column(String(80))
    risk_governance_drawdown_status: Mapped[str | None] = mapped_column(String(80))
    risk_governance_approved_before_caps: Mapped[bool | None] = mapped_column(Boolean)
    risk_governance_approved_after_caps: Mapped[bool | None] = mapped_column(Boolean)
    risk_governance_shadow_only: Mapped[bool | None] = mapped_column(Boolean)
    risk_governance_blocked: Mapped[bool | None] = mapped_column(Boolean)
    risk_governance_rank: Mapped[int | None] = mapped_column(Integer)
    risk_governance_rank_score: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    gate_diagnostics: Mapped[dict[str, object] | None] = mapped_column(JSON)
    gate_mapping_ok: Mapped[bool | None] = mapped_column(Boolean)
    gate_market_open: Mapped[bool | None] = mapped_column(Boolean)
    gate_game_not_started: Mapped[bool | None] = mapped_column(Boolean)
    gate_price_fresh_executable: Mapped[bool | None] = mapped_column(Boolean)
    gate_data_quality_ok: Mapped[bool | None] = mapped_column(Boolean)
    gate_push_ok: Mapped[bool | None] = mapped_column(Boolean)
    gate_probability_present: Mapped[bool | None] = mapped_column(Boolean)
    gate_gross_ev_positive: Mapped[bool | None] = mapped_column(Boolean)
    gate_fee_present: Mapped[bool | None] = mapped_column(Boolean)
    gate_probability_edge_ok: Mapped[bool | None] = mapped_column(Boolean)
    gate_net_ev_ok: Mapped[bool | None] = mapped_column(Boolean)
    gate_calibration_ok: Mapped[bool | None] = mapped_column(Boolean)
    gate_line_selection_ok: Mapped[bool | None] = mapped_column(Boolean)
    gate_caps_ok: Mapped[bool | None] = mapped_column(Boolean)
    gate_open_position_ok: Mapped[bool | None] = mapped_column(Boolean)
    gate_final_trade_eligible: Mapped[bool | None] = mapped_column(Boolean)
    blocked_by_quality_only: Mapped[bool | None] = mapped_column(Boolean)
    would_pass_ev_if_quality_allowed: Mapped[bool | None] = mapped_column(Boolean)
    would_pass_edge_if_quality_allowed: Mapped[bool | None] = mapped_column(Boolean)
    ev_edge_pass_but_quality_fail: Mapped[bool | None] = mapped_column(Boolean)
    counterfactual_trade_eligible_before_quality: Mapped[bool | None] = mapped_column(Boolean)
    counterfactual_trade_eligible_after_quality: Mapped[bool | None] = mapped_column(Boolean)
    bankroll_at_entry: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    risk_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))
    risk_dollars: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    contracts: Mapped[int | None] = mapped_column(Integer)
    estimated_cost_per_contract: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    estimated_total_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    one_contract_expected_value: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    sized_expected_value: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    one_contract_fee_estimate: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    total_fee_estimate: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))


class PaperTrade(TimestampMixin, Base):
    __tablename__ = "paper_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    paper_trading_epoch_id: Mapped[int | None] = mapped_column(ForeignKey("paper_trading_epochs.id"), index=True)
    candidate_id: Mapped[int | None] = mapped_column(ForeignKey("model_candidates.id"))
    market_ticker: Mapped[str] = mapped_column(String(120), nullable=False)
    contract_side: Mapped[str] = mapped_column(String(10), nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    current_price: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    exit_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_price_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(40), default="open", nullable=False)
    expected_value: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    realized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    resolution: Mapped[str | None] = mapped_column(String(40))
    fee_paid: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(String(40))
    market_display: Mapped[str | None] = mapped_column(Text)
    selection_display: Mapped[str | None] = mapped_column(String(40))
    matchup_display: Mapped[str | None] = mapped_column(String(80))
    contract_display: Mapped[str | None] = mapped_column(Text)
    market_family: Mapped[str | None] = mapped_column(String(80))
    line_value: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    selection_code: Mapped[str | None] = mapped_column(String(40))
    over_under_side: Mapped[str | None] = mapped_column(String(20))
    inning_scope: Mapped[str | None] = mapped_column(String(40))
    settlement_rule_status: Mapped[str | None] = mapped_column(String(80))
    economic_exposure_label: Mapped[str | None] = mapped_column(Text)
    economic_exposure_key: Mapped[str | None] = mapped_column(String(180))
    economic_exposure_family: Mapped[str | None] = mapped_column(String(40))
    economic_exposure_scope: Mapped[str | None] = mapped_column(String(40))
    economic_exposure_direction: Mapped[str | None] = mapped_column(String(40))
    economic_exposure_team: Mapped[str | None] = mapped_column(String(40))
    economic_exposure_line: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    contract_mechanics_label: Mapped[str | None] = mapped_column(Text)
    concept_cluster_key: Mapped[str | None] = mapped_column(String(160))
    same_game_concept_cluster_key: Mapped[str | None] = mapped_column(String(180))
    line_class: Mapped[str | None] = mapped_column(String(40))
    line_class_reason: Mapped[str | None] = mapped_column(String(120))
    line_ladder_rank: Mapped[int | None] = mapped_column(Integer)
    line_ladder_distance_from_central: Mapped[int | None] = mapped_column(Integer)
    line_ladder_size: Mapped[int | None] = mapped_column(Integer)
    exposure_taxonomy_version: Mapped[str | None] = mapped_column(String(80))
    line_classification_policy_version: Mapped[str | None] = mapped_column(String(80))
    selector_policy_version: Mapped[str | None] = mapped_column(String(80))
    selector_mode: Mapped[str | None] = mapped_column(String(40))
    selector_status: Mapped[str | None] = mapped_column(String(40))
    selector_decision: Mapped[str | None] = mapped_column(String(120))
    selector_rejection_reason: Mapped[str | None] = mapped_column(String(120))
    selector_threshold_profile: Mapped[str | None] = mapped_column(String(120))
    selector_min_net_ev: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    selector_min_prob_edge: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    selector_min_data_quality: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    selector_line_class_policy: Mapped[str | None] = mapped_column(String(120))
    selector_concept_cluster_key: Mapped[str | None] = mapped_column(String(160))
    selector_same_game_concept_cluster_key: Mapped[str | None] = mapped_column(String(180))
    selector_cluster_rank: Mapped[int | None] = mapped_column(Integer)
    selector_cluster_rank_score: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    selector_selected_from_cluster: Mapped[bool | None] = mapped_column(Boolean)
    selector_shadow_only: Mapped[bool | None] = mapped_column(Boolean)
    selector_live_like_eligible_before_cluster: Mapped[bool | None] = mapped_column(Boolean)
    selector_live_like_eligible_after_cluster: Mapped[bool | None] = mapped_column(Boolean)
    risk_governance_policy_version: Mapped[str | None] = mapped_column(String(120))
    risk_governance_enabled: Mapped[bool | None] = mapped_column(Boolean)
    risk_governance_status: Mapped[str | None] = mapped_column(String(80))
    risk_governance_decision: Mapped[str | None] = mapped_column(String(120))
    risk_governance_rejection_reason: Mapped[str | None] = mapped_column(String(160))
    risk_governance_family_status: Mapped[str | None] = mapped_column(String(80))
    risk_governance_family_cap_status: Mapped[str | None] = mapped_column(String(80))
    risk_governance_concept_cluster_cap_status: Mapped[str | None] = mapped_column(String(80))
    risk_governance_same_game_cap_status: Mapped[str | None] = mapped_column(String(80))
    risk_governance_alternate_line_cap_status: Mapped[str | None] = mapped_column(String(80))
    risk_governance_low_price_tail_cap_status: Mapped[str | None] = mapped_column(String(80))
    risk_governance_drawdown_status: Mapped[str | None] = mapped_column(String(80))
    risk_governance_approved_before_caps: Mapped[bool | None] = mapped_column(Boolean)
    risk_governance_approved_after_caps: Mapped[bool | None] = mapped_column(Boolean)
    risk_governance_shadow_only: Mapped[bool | None] = mapped_column(Boolean)
    risk_governance_blocked: Mapped[bool | None] = mapped_column(Boolean)
    risk_governance_rank: Mapped[int | None] = mapped_column(Integer)
    risk_governance_rank_score: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    training_eligible: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    bankroll_at_entry: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    risk_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))
    risk_dollars: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    estimated_cost_per_contract: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    estimated_total_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    one_contract_expected_value: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    sized_expected_value: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    one_contract_fee_estimate: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    total_fee_estimate: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))


class Order(TimestampMixin, Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kalshi_order_id: Mapped[str | None] = mapped_column(String(120), unique=True)
    kalshi_market_id: Mapped[int | None] = mapped_column(ForeignKey("kalshi_markets.id"))
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    action: Mapped[str] = mapped_column(String(10), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="created", nullable=False)
    live_order: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Fill(TimestampMixin, Base):
    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Position(TimestampMixin, Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kalshi_market_id: Mapped[int | None] = mapped_column(ForeignKey("kalshi_markets.id"))
    market_ticker: Mapped[str] = mapped_column(String(120), nullable=False)
    contract_side: Mapped[str] = mapped_column(String(10), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    current_price: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    status: Mapped[str] = mapped_column(String(40), default="open", nullable=False)
    resolution: Mapped[str | None] = mapped_column(String(40))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Settlement(TimestampMixin, Base):
    __tablename__ = "settlements"
    __table_args__ = (UniqueConstraint("paper_trade_id", name="uq_settlement_paper_trade"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    position_id: Mapped[int | None] = mapped_column(ForeignKey("positions.id"))
    paper_trade_id: Mapped[int | None] = mapped_column(ForeignKey("paper_trades.id"))
    settled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolution: Mapped[str] = mapped_column(String(40), nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(40))
    payout: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    fee_paid: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)


class TrainingRun(TimestampMixin, Base):
    __tablename__ = "training_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    model_version_id: Mapped[int | None] = mapped_column(ForeignKey("model_versions.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(40), default="running", nullable=False)
    candidate_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    metrics: Mapped[dict[str, object] | None] = mapped_column(JSON)


class CalibrationRun(TimestampMixin, Base):
    __tablename__ = "calibration_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    model_version_id: Mapped[int | None] = mapped_column(ForeignKey("model_versions.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(40), default="running", nullable=False)
    method: Mapped[str | None] = mapped_column(String(80))
    metrics: Mapped[dict[str, object] | None] = mapped_column(JSON)


class FeatureSnapshot(TimestampMixin, Base):
    __tablename__ = "feature_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    candidate_id: Mapped[int | None] = mapped_column(ForeignKey("model_candidates.id"))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    features: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    feature_version: Mapped[str | None] = mapped_column(String(80))
    source_statuses: Mapped[dict[str, object] | None] = mapped_column(JSON)


class MlbFeatureSnapshot(TimestampMixin, Base):
    __tablename__ = "mlb_feature_snapshots"
    __table_args__ = (
        UniqueConstraint("mlb_game_id", "target_date", "source", name="uq_mlb_feature_game_date_source"),
        Index("ix_mlb_feature_snapshots_date_source_captured", "target_date", "source", "captured_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mlb_game_id: Mapped[int | None] = mapped_column(ForeignKey("mlb_games.id"))
    target_date: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    data_quality: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    source_statuses: Mapped[dict[str, object] | None] = mapped_column(JSON)
    features: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)


class TeamDailyFeature(TimestampMixin, Base):
    __tablename__ = "team_daily_features"
    __table_args__ = (UniqueConstraint("target_date", "team_code", "source", name="uq_team_daily_date_team_source"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_date: Mapped[date] = mapped_column(Date, nullable=False)
    team_code: Mapped[str] = mapped_column(String(12), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    source_status: Mapped[str] = mapped_column(String(40), nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    completeness: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    stale: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    features: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    raw_payload: Mapped[dict[str, object] | None] = mapped_column(JSON)


class TeamRecentFeature(TimestampMixin, Base):
    __tablename__ = "team_recent_features"
    __table_args__ = (UniqueConstraint("target_date", "team_code", "window_days", "source", name="uq_team_recent_date_team_window_source"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_date: Mapped[date] = mapped_column(Date, nullable=False)
    team_code: Mapped[str] = mapped_column(String(12), nullable=False)
    window_days: Mapped[int] = mapped_column(Integer, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    source_status: Mapped[str] = mapped_column(String(40), nullable=False)
    sample_size: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    completeness: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    stale: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    features: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    raw_payload: Mapped[dict[str, object] | None] = mapped_column(JSON)


class PitcherDailyFeature(TimestampMixin, Base):
    __tablename__ = "pitcher_daily_features"
    __table_args__ = (UniqueConstraint("target_date", "team_code", "pitcher_id", "source", name="uq_pitcher_daily_date_team_player_source"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_date: Mapped[date] = mapped_column(Date, nullable=False)
    team_code: Mapped[str] = mapped_column(String(12), nullable=False)
    pitcher_id: Mapped[str] = mapped_column(String(40), nullable=False)
    pitcher_name: Mapped[str | None] = mapped_column(String(120))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    source_status: Mapped[str] = mapped_column(String(40), nullable=False)
    sample_size: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    completeness: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    stale: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    features: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    raw_payload: Mapped[dict[str, object] | None] = mapped_column(JSON)


class BullpenDailyFeature(TimestampMixin, Base):
    __tablename__ = "bullpen_daily_features"
    __table_args__ = (UniqueConstraint("target_date", "team_code", "source", name="uq_bullpen_daily_date_team_source"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_date: Mapped[date] = mapped_column(Date, nullable=False)
    team_code: Mapped[str] = mapped_column(String(12), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    source_status: Mapped[str] = mapped_column(String(40), nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    completeness: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    stale: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    features: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    raw_payload: Mapped[dict[str, object] | None] = mapped_column(JSON)


class LineupSnapshot(TimestampMixin, Base):
    __tablename__ = "lineup_snapshots"
    __table_args__ = (UniqueConstraint("mlb_game_id", "team_code", "source", name="uq_lineup_game_team_source"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mlb_game_id: Mapped[int | None] = mapped_column(ForeignKey("mlb_games.id"))
    target_date: Mapped[date] = mapped_column(Date, nullable=False)
    team_code: Mapped[str] = mapped_column(String(12), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lineup_posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    source_status: Mapped[str] = mapped_column(String(40), nullable=False)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    completeness: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    stale: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    features: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    raw_payload: Mapped[dict[str, object] | None] = mapped_column(JSON)


class InjurySnapshot(TimestampMixin, Base):
    __tablename__ = "injury_snapshots"
    __table_args__ = (UniqueConstraint("target_date", "team_code", "source", name="uq_injury_date_team_source"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_date: Mapped[date] = mapped_column(Date, nullable=False)
    team_code: Mapped[str] = mapped_column(String(12), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    source_status: Mapped[str] = mapped_column(String(40), nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    completeness: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    stale: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    features: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    raw_payload: Mapped[dict[str, object] | None] = mapped_column(JSON)


class WeatherSnapshot(TimestampMixin, Base):
    __tablename__ = "weather_snapshots"
    __table_args__ = (UniqueConstraint("mlb_game_id", "source", name="uq_weather_game_source"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mlb_game_id: Mapped[int | None] = mapped_column(ForeignKey("mlb_games.id"))
    target_date: Mapped[date] = mapped_column(Date, nullable=False)
    venue_name: Mapped[str | None] = mapped_column(String(120))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    forecast_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    source_status: Mapped[str] = mapped_column(String(40), nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    completeness: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    stale: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    features: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    raw_payload: Mapped[dict[str, object] | None] = mapped_column(JSON)


class ParkFactorSnapshot(TimestampMixin, Base):
    __tablename__ = "park_factor_snapshots"
    __table_args__ = (UniqueConstraint("venue_name", "source", name="uq_park_factor_venue_source"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    venue_name: Mapped[str] = mapped_column(String(120), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    source_status: Mapped[str] = mapped_column(String(40), nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    completeness: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    stale: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    features: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    raw_payload: Mapped[dict[str, object] | None] = mapped_column(JSON)


class TravelScheduleFeature(TimestampMixin, Base):
    __tablename__ = "travel_schedule_features"
    __table_args__ = (UniqueConstraint("mlb_game_id", "team_code", "source", name="uq_travel_game_team_source"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mlb_game_id: Mapped[int | None] = mapped_column(ForeignKey("mlb_games.id"))
    target_date: Mapped[date] = mapped_column(Date, nullable=False)
    team_code: Mapped[str] = mapped_column(String(12), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    source_status: Mapped[str] = mapped_column(String(40), nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    completeness: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    stale: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    features: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    raw_payload: Mapped[dict[str, object] | None] = mapped_column(JSON)


class ModelParameterVersion(TimestampMixin, Base):
    __tablename__ = "model_parameter_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version_tag: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    model_family: Mapped[str] = mapped_column(String(80), nullable=False)
    role: Mapped[str] = mapped_column(String(40), default="challenger", nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="created", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_reason: Mapped[str | None] = mapped_column(Text)
    trained_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_training_run_id: Mapped[int | None] = mapped_column(ForeignKey("training_runs.id"))
    parameters: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    metrics: Mapped[dict[str, object] | None] = mapped_column(JSON)


class ModelTrainingDataset(TimestampMixin, Base):
    __tablename__ = "model_training_datasets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    training_run_id: Mapped[int | None] = mapped_column(ForeignKey("training_runs.id"))
    created_at_snapshot: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    feature_version: Mapped[str] = mapped_column(String(80), nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    split_policy: Mapped[str] = mapped_column(String(80), nullable=False)
    filters: Mapped[dict[str, object] | None] = mapped_column(JSON)
    candidate_ids: Mapped[list[object] | None] = mapped_column(JSON)


class ModelThresholdVersion(TimestampMixin, Base):
    __tablename__ = "model_threshold_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version_tag: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    role: Mapped[str] = mapped_column(String(40), default="evaluation", nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="recorded", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at_snapshot: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_training_run_id: Mapped[int | None] = mapped_column(ForeignKey("training_runs.id"))
    thresholds: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    metrics: Mapped[dict[str, object] | None] = mapped_column(JSON)


class ModelPredictionRun(TimestampMixin, Base):
    __tablename__ = "model_prediction_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    paper_trading_epoch_id: Mapped[int | None] = mapped_column(ForeignKey("paper_trading_epochs.id"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    target_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(40), default="running", nullable=False)
    model_version_tag: Mapped[str | None] = mapped_column(String(120))
    feature_version: Mapped[str | None] = mapped_column(String(80))
    candidates_evaluated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    trades_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    trade_policy: Mapped[dict[str, object] | None] = mapped_column(JSON)
    summary: Mapped[dict[str, object] | None] = mapped_column(JSON)


class ModelPredictionOutput(TimestampMixin, Base):
    __tablename__ = "model_prediction_outputs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    paper_trading_epoch_id: Mapped[int | None] = mapped_column(ForeignKey("paper_trading_epochs.id"), index=True)
    prediction_run_id: Mapped[int | None] = mapped_column(ForeignKey("model_prediction_runs.id"))
    candidate_id: Mapped[int | None] = mapped_column(ForeignKey("model_candidates.id"))
    market_family: Mapped[str | None] = mapped_column(String(80))
    probability_raw: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))
    probability_calibrated: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))
    fair_value: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    executable_price: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    expected_value_gross: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    fee_estimate: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    expected_value_net: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    probability_edge: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    executable_price_source: Mapped[str | None] = mapped_column(String(80))
    price_status: Mapped[str | None] = mapped_column(String(80))
    data_quality: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    calibration_status: Mapped[str | None] = mapped_column(String(80))
    trade_rank: Mapped[int | None] = mapped_column(Integer)
    decision_reason: Mapped[str | None] = mapped_column(String(DECISION_REASON_MAX_LENGTH))
    raw_output: Mapped[dict[str, object] | None] = mapped_column(JSON)


class JobRun(TimestampMixin, Base):
    __tablename__ = "job_runs"
    __table_args__ = (
        Index("ix_job_runs_name_date_status", "job_name", "target_date", "status"),
        Index("ix_job_runs_lock_status", "lock_key", "status"),
        Index("ix_job_runs_started_at", "started_at"),
        Index("ix_job_runs_epoch", "paper_trading_epoch_id"),
        Index("ix_job_runs_epoch_name_started_id", "paper_trading_epoch_id", "job_name", "started_at", "id"),
        Index(
            "uq_job_runs_running_lock_key",
            "lock_key",
            unique=True,
            postgresql_where=text("status = 'running'"),
            sqlite_where=text("status = 'running'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_name: Mapped[str] = mapped_column(String(120), nullable=False)
    job_type: Mapped[str] = mapped_column(String(80), nullable=False)
    target_date: Mapped[date | None] = mapped_column(Date)
    paper_trading_epoch_id: Mapped[int | None] = mapped_column(ForeignKey("paper_trading_epochs.id"))
    status: Mapped[str] = mapped_column(String(40), default="running", nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    lock_key: Mapped[str | None] = mapped_column(String(180))
    triggered_by: Mapped[str] = mapped_column(String(40), default="manual", nullable=False)
    steps: Mapped[list[object] | None] = mapped_column(JSON)
    result: Mapped[dict[str, object] | None] = mapped_column(JSON)
    warnings: Mapped[list[object] | None] = mapped_column(JSON)
    errors: Mapped[list[object] | None] = mapped_column(JSON)
    idempotency_key: Mapped[str | None] = mapped_column(String(180))


class MarketDataWorkerStatus(TimestampMixin, Base):
    __tablename__ = "market_data_worker_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status_key: Mapped[str] = mapped_column(String(80), unique=True, default="kalshi_ws_paper", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    running: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source: Mapped[str] = mapped_column(String(40), default="rest_fallback", nullable=False)
    subscribed_market_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconnect_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    stale_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    raw_status: Mapped[dict[str, object] | None] = mapped_column(JSON)


class ModelGovernanceEvent(TimestampMixin, Base):
    __tablename__ = "model_governance_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    details: Mapped[dict[str, object] | None] = mapped_column(JSON)


class RiskEvent(TimestampMixin, Base):
    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    severity: Mapped[str] = mapped_column(String(40), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    event_metadata: Mapped[dict[str, object] | None] = mapped_column("metadata", JSON)


class MarketFamilyDiscoveryRun(TimestampMixin, Base):
    __tablename__ = "market_family_discovery_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_date: Mapped[date] = mapped_column(Date, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(40), default="running", nullable=False)
    games_considered: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    families_considered: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    markets_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    errors: Mapped[list[object] | None] = mapped_column(JSON)
    warnings: Mapped[list[object] | None] = mapped_column(JSON)
    raw_summary: Mapped[dict[str, object] | None] = mapped_column(JSON)


class MarketFamilyDiscoveryItem(TimestampMixin, Base):
    __tablename__ = "market_family_discovery_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("market_family_discovery_runs.id"), nullable=False)
    mlb_game_id: Mapped[int | None] = mapped_column(ForeignKey("mlb_games.id"))
    family_key: Mapped[str] = mapped_column(String(80), nullable=False)
    candidate_series_ticker: Mapped[str | None] = mapped_column(String(120))
    candidate_event_ticker: Mapped[str | None] = mapped_column(String(120))
    candidate_market_ticker: Mapped[str | None] = mapped_column(String(120))
    returned_ticker: Mapped[str | None] = mapped_column(String(120))
    returned_event_ticker: Mapped[str | None] = mapped_column(String(120))
    title: Mapped[str | None] = mapped_column(Text)
    subtitle: Mapped[str | None] = mapped_column(Text)
    yes_sub_title: Mapped[str | None] = mapped_column(Text)
    no_sub_title: Mapped[str | None] = mapped_column(Text)
    rules_primary: Mapped[str | None] = mapped_column(Text)
    rules_secondary: Mapped[str | None] = mapped_column(Text)
    custom_strike: Mapped[dict[str, object] | None] = mapped_column(JSON)
    functional_strike: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(String(40))
    raw_status: Mapped[str | None] = mapped_column(String(40))
    validation_status: Mapped[str | None] = mapped_column(String(80))
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    line_value: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    selection_code: Mapped[str | None] = mapped_column(String(40))
    source_strategy: Mapped[str | None] = mapped_column(String(80))
    raw_payload: Mapped[dict[str, object] | None] = mapped_column(JSON)
