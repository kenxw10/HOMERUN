"""Initial HOMERUN schema.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-06-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def timestamp_columns() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "bot_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(length=120), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("description", sa.Text()),
        *timestamp_columns(),
    )
    op.create_index("ix_bot_settings_key", "bot_settings", ["key"], unique=True)

    op.create_table(
        "balance_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cash_balance", sa.Numeric(14, 2), nullable=False),
        sa.Column("portfolio_value", sa.Numeric(14, 2), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False, server_default="paper"),
        *timestamp_columns(),
    )

    op.create_table(
        "mlb_games",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("external_game_id", sa.String(length=120), nullable=False),
        sa.Column("home_team", sa.String(length=120), nullable=False),
        sa.Column("away_team", sa.String(length=120), nullable=False),
        sa.Column("scheduled_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="scheduled"),
        sa.Column("home_score", sa.Integer()),
        sa.Column("away_score", sa.Integer()),
        *timestamp_columns(),
        sa.UniqueConstraint("external_game_id", name="uq_mlb_games_external_game_id"),
    )

    op.create_table(
        "kalshi_markets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kalshi_market_id", sa.String(length=120), nullable=False),
        sa.Column("ticker", sa.String(length=120), nullable=False),
        sa.Column("event_ticker", sa.String(length=120)),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("yes_bid", sa.Numeric(8, 4)),
        sa.Column("yes_ask", sa.Numeric(8, 4)),
        sa.Column("yes_mid", sa.Numeric(8, 4)),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="untracked"),
        sa.Column("close_time", sa.DateTime(timezone=True)),
        sa.Column("resolve_time", sa.DateTime(timezone=True)),
        *timestamp_columns(),
        sa.UniqueConstraint("kalshi_market_id", name="uq_kalshi_markets_kalshi_market_id"),
        sa.UniqueConstraint("ticker", name="uq_kalshi_markets_ticker"),
    )

    op.create_table(
        "model_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version_tag", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("trained_at", sa.DateTime(timezone=True)),
        sa.Column("metrics", sa.JSON()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        *timestamp_columns(),
        sa.UniqueConstraint("version_tag", name="uq_model_versions_version_tag"),
    )

    op.create_table(
        "market_mappings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mlb_game_id", sa.Integer(), sa.ForeignKey("mlb_games.id"), nullable=False),
        sa.Column("kalshi_market_id", sa.Integer(), sa.ForeignKey("kalshi_markets.id"), nullable=False),
        sa.Column("mapping_status", sa.String(length=40), nullable=False, server_default="candidate"),
        sa.Column("confidence", sa.Numeric(6, 4)),
        *timestamp_columns(),
        sa.UniqueConstraint("mlb_game_id", "kalshi_market_id", name="uq_game_market"),
    )

    op.create_table(
        "model_candidates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mlb_game_id", sa.Integer(), sa.ForeignKey("mlb_games.id")),
        sa.Column("kalshi_market_id", sa.Integer(), sa.ForeignKey("kalshi_markets.id")),
        sa.Column("model_version_id", sa.Integer(), sa.ForeignKey("model_versions.id")),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("features", sa.JSON(), nullable=False),
        sa.Column("probability", sa.Numeric(8, 6)),
        sa.Column("fair_value", sa.Numeric(8, 4)),
        sa.Column("market_price", sa.Numeric(8, 4)),
        sa.Column("expected_value", sa.Numeric(10, 6)),
        sa.Column("decision", sa.String(length=40), nullable=False, server_default="no_trade"),
        sa.Column("outcome", sa.String(length=40)),
        *timestamp_columns(),
    )

    op.create_table(
        "paper_trades",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("candidate_id", sa.Integer(), sa.ForeignKey("model_candidates.id")),
        sa.Column("market_ticker", sa.String(length=120), nullable=False),
        sa.Column("contract_side", sa.String(length=10), nullable=False),
        sa.Column("entry_price", sa.Numeric(8, 4), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("entry_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exit_price", sa.Numeric(8, 4)),
        sa.Column("exit_time", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="open"),
        sa.Column("realized_pnl", sa.Numeric(14, 2)),
        *timestamp_columns(),
    )

    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kalshi_order_id", sa.String(length=120), unique=True),
        sa.Column("kalshi_market_id", sa.Integer(), sa.ForeignKey("kalshi_markets.id")),
        sa.Column("side", sa.String(length=10), nullable=False),
        sa.Column("action", sa.String(length=10), nullable=False),
        sa.Column("price", sa.Numeric(8, 4), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="created"),
        sa.Column("live_order", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        *timestamp_columns(),
    )

    op.create_table(
        "fills",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("price", sa.Numeric(8, 4), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False),
        *timestamp_columns(),
    )

    op.create_table(
        "positions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kalshi_market_id", sa.Integer(), sa.ForeignKey("kalshi_markets.id")),
        sa.Column("market_ticker", sa.String(length=120), nullable=False),
        sa.Column("contract_side", sa.String(length=10), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("entry_price", sa.Numeric(8, 4), nullable=False),
        sa.Column("current_price", sa.Numeric(8, 4)),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="open"),
        sa.Column("resolution", sa.String(length=40)),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        *timestamp_columns(),
    )

    op.create_table(
        "settlements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("position_id", sa.Integer(), sa.ForeignKey("positions.id"), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolution", sa.String(length=40), nullable=False),
        sa.Column("payout", sa.Numeric(14, 2), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(14, 2), nullable=False),
        *timestamp_columns(),
    )

    op.create_table(
        "training_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("model_version_id", sa.Integer(), sa.ForeignKey("model_versions.id")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="running"),
        sa.Column("candidate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metrics", sa.JSON()),
        *timestamp_columns(),
    )

    op.create_table(
        "calibration_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("model_version_id", sa.Integer(), sa.ForeignKey("model_versions.id")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="running"),
        sa.Column("method", sa.String(length=80)),
        sa.Column("metrics", sa.JSON()),
        *timestamp_columns(),
    )

    op.create_table(
        "feature_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("candidate_id", sa.Integer(), sa.ForeignKey("model_candidates.id")),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("features", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=False),
        *timestamp_columns(),
    )

    op.create_table(
        "risk_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("severity", sa.String(length=40), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("metadata", sa.JSON()),
        *timestamp_columns(),
    )


def downgrade() -> None:
    op.drop_table("risk_events")
    op.drop_table("feature_snapshots")
    op.drop_table("calibration_runs")
    op.drop_table("training_runs")
    op.drop_table("settlements")
    op.drop_table("positions")
    op.drop_table("fills")
    op.drop_table("orders")
    op.drop_table("paper_trades")
    op.drop_table("model_candidates")
    op.drop_table("market_mappings")
    op.drop_table("model_versions")
    op.drop_table("kalshi_markets")
    op.drop_table("mlb_games")
    op.drop_table("balance_snapshots")
    op.drop_index("ix_bot_settings_key", table_name="bot_settings")
    op.drop_table("bot_settings")
