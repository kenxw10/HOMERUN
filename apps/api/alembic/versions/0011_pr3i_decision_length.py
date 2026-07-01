"""PR3i widen candidate decision field.

Revision ID: 0011_pr3i_decision_length
Revises: 0010_pr3d_paper_ops
Create Date: 2026-07-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_pr3i_decision_length"
down_revision: str | None = "0010_pr3d_paper_ops"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _set_fast_postgres_timeouts() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("SET lock_timeout = '5s'")
        op.execute("SET statement_timeout = '30s'")


def upgrade() -> None:
    _set_fast_postgres_timeouts()

    with op.batch_alter_table("model_candidates") as batch_op:
        batch_op.alter_column(
            "decision",
            existing_type=sa.String(length=40),
            type_=sa.String(length=120),
            existing_nullable=False,
            existing_server_default=None,
        )


def downgrade() -> None:
    _set_fast_postgres_timeouts()

    with op.batch_alter_table("model_candidates") as batch_op:
        batch_op.alter_column(
            "decision",
            existing_type=sa.String(length=120),
            type_=sa.String(length=40),
            existing_nullable=False,
            existing_server_default=None,
        )
