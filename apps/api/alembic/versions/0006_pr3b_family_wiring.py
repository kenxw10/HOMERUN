"""PR3b market-family paper wiring metadata.

Revision ID: 0006_pr3b_family_wiring
Revises: 0005_pr3a_discovery
Create Date: 2026-06-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_pr3b_family_wiring"
down_revision: str | None = "0005_pr3a_discovery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _set_fast_postgres_timeouts() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("SET lock_timeout = '5s'")
        op.execute("SET statement_timeout = '30s'")


FAMILY_COLUMNS = (
    sa.Column("market_family", sa.String(length=80)),
    sa.Column("market_type", sa.String(length=80)),
    sa.Column("line_value", sa.Numeric(10, 4)),
    sa.Column("selection_code", sa.String(length=40)),
    sa.Column("over_under_side", sa.String(length=20)),
    sa.Column("inning_scope", sa.String(length=40)),
    sa.Column("settlement_rule_status", sa.String(length=80)),
)

TRADE_FAMILY_COLUMNS = (
    sa.Column("market_family", sa.String(length=80)),
    sa.Column("line_value", sa.Numeric(10, 4)),
    sa.Column("selection_code", sa.String(length=40)),
    sa.Column("over_under_side", sa.String(length=20)),
    sa.Column("inning_scope", sa.String(length=40)),
    sa.Column("settlement_rule_status", sa.String(length=80)),
)


def upgrade() -> None:
    _set_fast_postgres_timeouts()

    for column in FAMILY_COLUMNS:
        op.add_column("kalshi_markets", column.copy())
    for column in FAMILY_COLUMNS:
        op.add_column("market_mappings", column.copy())

    op.add_column("model_candidates", sa.Column("feature_version", sa.String(length=80)))
    op.add_column(
        "model_candidates",
        sa.Column("training_eligible", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    for column in TRADE_FAMILY_COLUMNS:
        op.add_column("model_candidates", column.copy())

    for column in TRADE_FAMILY_COLUMNS:
        op.add_column("paper_trades", column.copy())
    op.add_column(
        "paper_trades",
        sa.Column("training_eligible", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    _set_fast_postgres_timeouts()

    op.drop_column("paper_trades", "training_eligible")
    for column_name in reversed([column.name for column in TRADE_FAMILY_COLUMNS]):
        op.drop_column("paper_trades", column_name)

    for column_name in reversed([column.name for column in TRADE_FAMILY_COLUMNS]):
        op.drop_column("model_candidates", column_name)
    op.drop_column("model_candidates", "training_eligible")
    op.drop_column("model_candidates", "feature_version")

    for column_name in reversed([column.name for column in FAMILY_COLUMNS]):
        op.drop_column("market_mappings", column_name)
    for column_name in reversed([column.name for column in FAMILY_COLUMNS]):
        op.drop_column("kalshi_markets", column_name)
