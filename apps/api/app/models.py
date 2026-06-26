from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, JSON, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


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


class BalanceSnapshot(TimestampMixin, Base):
    __tablename__ = "balance_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
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

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mlb_game_id: Mapped[int | None] = mapped_column(ForeignKey("mlb_games.id"))
    kalshi_market_id: Mapped[int | None] = mapped_column(ForeignKey("kalshi_markets.id"))
    mapping_id: Mapped[int | None] = mapped_column(ForeignKey("market_mappings.id"))
    model_version_id: Mapped[int | None] = mapped_column(ForeignKey("model_versions.id"))
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    features: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    probability: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))
    model_probability: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))
    fair_value: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    market_price: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    executable_price: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    expected_value: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    fee_estimate: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    net_expected_value: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    market_type: Mapped[str | None] = mapped_column(String(80))
    time_bucket: Mapped[str | None] = mapped_column(String(40))
    time_to_start_minutes: Mapped[int | None] = mapped_column(Integer)
    contract_side: Mapped[str | None] = mapped_column(String(10))
    decision: Mapped[str] = mapped_column(String(40), default="no_trade", nullable=False)
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
    market_family: Mapped[str | None] = mapped_column(String(80))
    line_value: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    selection_code: Mapped[str | None] = mapped_column(String(40))
    over_under_side: Mapped[str | None] = mapped_column(String(20))
    inning_scope: Mapped[str | None] = mapped_column(String(40))
    settlement_rule_status: Mapped[str | None] = mapped_column(String(80))


class PaperTrade(TimestampMixin, Base):
    __tablename__ = "paper_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
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
    training_eligible: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


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
