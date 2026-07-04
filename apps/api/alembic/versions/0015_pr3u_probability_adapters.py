"""PR3u add candidate probability adapter metadata columns.

Revision ID: 0015_pr3u_probability_adapters
Revises: 0014_pr3t_live_like_selector
Create Date: 2026-07-04
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0015_pr3u_probability_adapters"
down_revision: str | None = "0014_pr3t_live_like_selector"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


PROBABILITY_ADAPTER_COLUMN_SPECS = (
    ("probability_adapter_key", sa.String(length=120)),
    ("probability_adapter_version", sa.String(length=120)),
    ("probability_adapter_policy_version", sa.String(length=120)),
    ("probability_adapter_family", sa.String(length=80)),
    ("probability_adapter_scope", sa.String(length=40)),
    ("probability_adapter_rationale", sa.Text()),
    ("probability_adapter_calibration_hook", sa.String(length=120)),
    ("probability_adapter_calibration_version", sa.String(length=120)),
    ("probability_adapter_feature_policy_version", sa.String(length=120)),
    ("probability_adapter_metadata", sa.JSON()),
)


def _set_fast_postgres_timeouts() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("SET lock_timeout = '5s'")
        op.execute("SET statement_timeout = '30s'")


def upgrade() -> None:
    _set_fast_postgres_timeouts()
    for name, column_type in PROBABILITY_ADAPTER_COLUMN_SPECS:
        op.add_column("model_candidates", sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    _set_fast_postgres_timeouts()
    for name, _column_type in reversed(PROBABILITY_ADAPTER_COLUMN_SPECS):
        op.drop_column("model_candidates", name)
