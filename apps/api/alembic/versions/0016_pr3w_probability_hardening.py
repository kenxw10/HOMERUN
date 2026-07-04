"""PR3w add candidate probability hardening metadata columns.

Revision ID: 0016_pr3w_probability_hardening
Revises: 0015_pr3u_probability_adapters
Create Date: 2026-07-04
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0016_pr3w_probability_hardening"
down_revision: str | None = "0015_pr3u_probability_adapters"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


PROBABILITY_HARDENING_COLUMN_SPECS = (
    ("probability_hardening_policy_version", sa.String(length=120)),
    ("probability_hardening_enabled", sa.Boolean()),
    ("probability_raw_adapter", sa.Numeric(8, 6)),
    ("probability_before_hardening", sa.Numeric(8, 6)),
    ("probability_after_hardening", sa.Numeric(8, 6)),
    ("probability_hardening_delta", sa.Numeric(10, 6)),
    ("probability_hardening_applied", sa.Boolean()),
    ("probability_hardening_reason", sa.String(length=160)),
    ("probability_hardening_status", sa.String(length=80)),
    ("probability_hardening_line_class", sa.String(length=40)),
    ("probability_hardening_line_class_policy", sa.String(length=120)),
    ("probability_hardening_consistency_status", sa.String(length=80)),
    ("probability_hardening_monotonicity_status", sa.String(length=80)),
    ("probability_hardening_ladder_role", sa.String(length=80)),
    ("probability_hardening_ladder_size", sa.Integer()),
    ("probability_hardening_ladder_rank", sa.Integer()),
    ("probability_hardening_distance_from_central", sa.Integer()),
    ("probability_hardening_central_reference_line", sa.Numeric(10, 4)),
    ("probability_hardening_central_reference_probability", sa.Numeric(8, 6)),
    ("probability_hardening_dampening_factor", sa.Numeric(6, 4)),
    ("probability_hardening_shadow_only", sa.Boolean()),
    ("probability_hardening_block_recommendation", sa.Boolean()),
    ("probability_hardening_error_reason", sa.String(length=160)),
)


def _set_fast_postgres_timeouts() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("SET lock_timeout = '5s'")
        op.execute("SET statement_timeout = '30s'")


def upgrade() -> None:
    _set_fast_postgres_timeouts()
    for name, column_type in PROBABILITY_HARDENING_COLUMN_SPECS:
        op.add_column("model_candidates", sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    _set_fast_postgres_timeouts()
    for name, _column_type in reversed(PROBABILITY_HARDENING_COLUMN_SPECS):
        op.drop_column("model_candidates", name)
