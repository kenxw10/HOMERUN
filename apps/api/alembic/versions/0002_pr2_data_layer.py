"""PR2 data layer columns.

Revision ID: 0002_pr2_data_layer
Revises: 0001_initial_schema
Create Date: 2026-06-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_pr2_data_layer"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("mlb_games", sa.Column("raw_payload", sa.JSON()))

    op.add_column("kalshi_markets", sa.Column("subtitle", sa.Text()))
    op.add_column("kalshi_markets", sa.Column("rules", sa.Text()))
    op.add_column("kalshi_markets", sa.Column("yes_subtitle", sa.Text()))
    op.add_column("kalshi_markets", sa.Column("no_subtitle", sa.Text()))
    op.add_column("kalshi_markets", sa.Column("no_bid", sa.Numeric(8, 4)))
    op.add_column("kalshi_markets", sa.Column("no_ask", sa.Numeric(8, 4)))
    op.add_column("kalshi_markets", sa.Column("no_mid", sa.Numeric(8, 4)))
    op.add_column("kalshi_markets", sa.Column("last_price", sa.Numeric(8, 4)))
    op.add_column("kalshi_markets", sa.Column("best_yes_bid", sa.Numeric(8, 4)))
    op.add_column("kalshi_markets", sa.Column("best_no_bid", sa.Numeric(8, 4)))
    op.add_column("kalshi_markets", sa.Column("implied_yes_ask", sa.Numeric(8, 4)))
    op.add_column("kalshi_markets", sa.Column("implied_no_ask", sa.Numeric(8, 4)))
    op.add_column("kalshi_markets", sa.Column("open_time", sa.DateTime(timezone=True)))
    op.add_column("kalshi_markets", sa.Column("occurrence_datetime", sa.DateTime(timezone=True)))
    op.add_column("kalshi_markets", sa.Column("raw_payload", sa.JSON()))
    op.add_column("kalshi_markets", sa.Column("orderbook_raw", sa.JSON()))

    op.add_column("market_mappings", sa.Column("rationale", sa.Text()))
    op.add_column("market_mappings", sa.Column("mapping_metadata", sa.JSON()))

    op.add_column("model_candidates", sa.Column("mapping_id", sa.Integer(), sa.ForeignKey("market_mappings.id")))
    op.add_column("model_candidates", sa.Column("model_probability", sa.Numeric(8, 6)))
    op.add_column("model_candidates", sa.Column("executable_price", sa.Numeric(8, 4)))
    op.add_column("model_candidates", sa.Column("fee_estimate", sa.Numeric(10, 6)))
    op.add_column("model_candidates", sa.Column("net_expected_value", sa.Numeric(10, 6)))
    op.add_column("model_candidates", sa.Column("market_type", sa.String(length=80)))
    op.add_column("model_candidates", sa.Column("time_bucket", sa.String(length=40)))
    op.add_column("model_candidates", sa.Column("time_to_start_minutes", sa.Integer()))
    op.add_column("model_candidates", sa.Column("contract_side", sa.String(length=10)))

    op.add_column("paper_trades", sa.Column("current_price", sa.Numeric(8, 4)))
    op.add_column("paper_trades", sa.Column("expected_value", sa.Numeric(10, 6)))


def downgrade() -> None:
    op.drop_column("paper_trades", "expected_value")
    op.drop_column("paper_trades", "current_price")

    op.drop_column("model_candidates", "contract_side")
    op.drop_column("model_candidates", "time_to_start_minutes")
    op.drop_column("model_candidates", "time_bucket")
    op.drop_column("model_candidates", "market_type")
    op.drop_column("model_candidates", "net_expected_value")
    op.drop_column("model_candidates", "fee_estimate")
    op.drop_column("model_candidates", "executable_price")
    op.drop_column("model_candidates", "model_probability")
    op.drop_column("model_candidates", "mapping_id")

    op.drop_column("market_mappings", "mapping_metadata")
    op.drop_column("market_mappings", "rationale")

    op.drop_column("kalshi_markets", "orderbook_raw")
    op.drop_column("kalshi_markets", "raw_payload")
    op.drop_column("kalshi_markets", "occurrence_datetime")
    op.drop_column("kalshi_markets", "open_time")
    op.drop_column("kalshi_markets", "implied_no_ask")
    op.drop_column("kalshi_markets", "implied_yes_ask")
    op.drop_column("kalshi_markets", "best_no_bid")
    op.drop_column("kalshi_markets", "best_yes_bid")
    op.drop_column("kalshi_markets", "last_price")
    op.drop_column("kalshi_markets", "no_mid")
    op.drop_column("kalshi_markets", "no_ask")
    op.drop_column("kalshi_markets", "no_bid")
    op.drop_column("kalshi_markets", "no_subtitle")
    op.drop_column("kalshi_markets", "yes_subtitle")
    op.drop_column("kalshi_markets", "rules")
    op.drop_column("kalshi_markets", "subtitle")

    op.drop_column("mlb_games", "raw_payload")
