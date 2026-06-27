"""PR3c hotfix fee-aware target-date candidates.

Revision ID: 0008_pr3c_fee_date_scope
Revises: 0007_pr3c_model_governance
Create Date: 2026-06-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_pr3c_fee_date_scope"
down_revision: str | None = "0007_pr3c_model_governance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _set_fast_postgres_timeouts() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("SET lock_timeout = '5s'")
        op.execute("SET statement_timeout = '30s'")


def upgrade() -> None:
    _set_fast_postgres_timeouts()

    op.add_column("model_candidates", sa.Column("probability_edge", sa.Numeric(10, 6)))
    op.add_column("model_candidates", sa.Column("target_date", sa.Date()))
    op.add_column("model_candidates", sa.Column("executable_price_source", sa.String(length=80)))
    op.add_column("model_candidates", sa.Column("market_price_updated_at", sa.DateTime(timezone=True)))
    op.add_column("model_candidates", sa.Column("price_staleness_seconds", sa.Integer()))
    op.add_column("model_candidates", sa.Column("price_status", sa.String(length=80)))

    op.add_column("model_prediction_outputs", sa.Column("executable_price", sa.Numeric(8, 4)))
    op.add_column("model_prediction_outputs", sa.Column("expected_value_gross", sa.Numeric(10, 6)))
    op.add_column("model_prediction_outputs", sa.Column("fee_estimate", sa.Numeric(10, 6)))
    op.add_column("model_prediction_outputs", sa.Column("expected_value_net", sa.Numeric(10, 6)))
    op.add_column("model_prediction_outputs", sa.Column("probability_edge", sa.Numeric(10, 6)))
    op.add_column("model_prediction_outputs", sa.Column("executable_price_source", sa.String(length=80)))
    op.add_column("model_prediction_outputs", sa.Column("price_status", sa.String(length=80)))


def downgrade() -> None:
    _set_fast_postgres_timeouts()

    op.drop_column("model_prediction_outputs", "price_status")
    op.drop_column("model_prediction_outputs", "executable_price_source")
    op.drop_column("model_prediction_outputs", "probability_edge")
    op.drop_column("model_prediction_outputs", "expected_value_net")
    op.drop_column("model_prediction_outputs", "fee_estimate")
    op.drop_column("model_prediction_outputs", "expected_value_gross")
    op.drop_column("model_prediction_outputs", "executable_price")

    op.drop_column("model_candidates", "price_status")
    op.drop_column("model_candidates", "price_staleness_seconds")
    op.drop_column("model_candidates", "market_price_updated_at")
    op.drop_column("model_candidates", "executable_price_source")
    op.drop_column("model_candidates", "target_date")
    op.drop_column("model_candidates", "probability_edge")
