"""PR3a market-family discovery and paper mark timestamps.

Revision ID: 0005_pr3a_discovery
Revises: 0004_pr3_results_model
Create Date: 2026-06-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_pr3a_discovery"
down_revision: str | None = "0004_pr3_results_model"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _set_fast_postgres_timeouts() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("SET lock_timeout = '5s'")
        op.execute("SET statement_timeout = '30s'")


def upgrade() -> None:
    _set_fast_postgres_timeouts()

    op.add_column("paper_trades", sa.Column("current_price_updated_at", sa.DateTime(timezone=True)))

    op.create_table(
        "market_family_discovery_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="running"),
        sa.Column("games_considered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("families_considered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("markets_found", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("errors", sa.JSON()),
        sa.Column("warnings", sa.JSON()),
        sa.Column("raw_summary", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_market_family_discovery_runs_target_date",
        "market_family_discovery_runs",
        ["target_date"],
    )

    op.create_table(
        "market_family_discovery_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("mlb_game_id", sa.Integer()),
        sa.Column("family_key", sa.String(length=80), nullable=False),
        sa.Column("candidate_series_ticker", sa.String(length=120)),
        sa.Column("candidate_event_ticker", sa.String(length=120)),
        sa.Column("candidate_market_ticker", sa.String(length=120)),
        sa.Column("returned_ticker", sa.String(length=120)),
        sa.Column("returned_event_ticker", sa.String(length=120)),
        sa.Column("title", sa.Text()),
        sa.Column("subtitle", sa.Text()),
        sa.Column("yes_sub_title", sa.Text()),
        sa.Column("no_sub_title", sa.Text()),
        sa.Column("rules_primary", sa.Text()),
        sa.Column("rules_secondary", sa.Text()),
        sa.Column("custom_strike", sa.JSON()),
        sa.Column("functional_strike", sa.Text()),
        sa.Column("status", sa.String(length=40)),
        sa.Column("raw_status", sa.String(length=40)),
        sa.Column("validation_status", sa.String(length=80)),
        sa.Column("confidence", sa.Numeric(6, 4)),
        sa.Column("line_value", sa.Numeric(10, 4)),
        sa.Column("selection_code", sa.String(length=40)),
        sa.Column("source_strategy", sa.String(length=80)),
        sa.Column("raw_payload", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["market_family_discovery_runs.id"]),
        sa.ForeignKeyConstraint(["mlb_game_id"], ["mlb_games.id"]),
    )
    op.create_index(
        "ix_market_family_discovery_items_run_family",
        "market_family_discovery_items",
        ["run_id", "family_key"],
    )


def downgrade() -> None:
    _set_fast_postgres_timeouts()

    op.drop_index("ix_market_family_discovery_items_run_family", table_name="market_family_discovery_items")
    op.drop_table("market_family_discovery_items")
    op.drop_index("ix_market_family_discovery_runs_target_date", table_name="market_family_discovery_runs")
    op.drop_table("market_family_discovery_runs")
    op.drop_column("paper_trades", "current_price_updated_at")
