"""PR2.5 targeted Kalshi resolver audit fields.

Revision ID: 0003_pr2_5_targeted_kalshi_resolver
Revises: 0002_pr2_data_layer
Create Date: 2026-06-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_pr2_5_targeted_kalshi_resolver"
down_revision: str | None = "0002_pr2_data_layer"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("mlb_games", sa.Column("home_abbreviation", sa.String(length=12)))
    op.add_column("mlb_games", sa.Column("away_abbreviation", sa.String(length=12)))
    op.add_column("kalshi_markets", sa.Column("raw_status", sa.String(length=40)))
    op.add_column("market_mappings", sa.Column("resolver_strategy", sa.String(length=80)))
    op.add_column("market_mappings", sa.Column("validation_status", sa.String(length=80)))


def downgrade() -> None:
    op.drop_column("market_mappings", "validation_status")
    op.drop_column("market_mappings", "resolver_strategy")
    op.drop_column("kalshi_markets", "raw_status")
    op.drop_column("mlb_games", "away_abbreviation")
    op.drop_column("mlb_games", "home_abbreviation")
