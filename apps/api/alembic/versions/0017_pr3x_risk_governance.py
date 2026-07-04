"""PR3x add paper risk governance metadata columns.

Revision ID: 0017_pr3x_risk_governance
Revises: 0016_pr3w_probability_hardening
Create Date: 2026-07-04
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0017_pr3x_risk_governance"
down_revision: str | None = "0016_pr3w_probability_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


RISK_GOVERNANCE_COLUMN_SPECS = (
    ("risk_governance_policy_version", sa.String(length=120)),
    ("risk_governance_enabled", sa.Boolean()),
    ("risk_governance_status", sa.String(length=80)),
    ("risk_governance_decision", sa.String(length=120)),
    ("risk_governance_rejection_reason", sa.String(length=160)),
    ("risk_governance_family_status", sa.String(length=80)),
    ("risk_governance_family_cap_status", sa.String(length=80)),
    ("risk_governance_concept_cluster_cap_status", sa.String(length=80)),
    ("risk_governance_same_game_cap_status", sa.String(length=80)),
    ("risk_governance_alternate_line_cap_status", sa.String(length=80)),
    ("risk_governance_low_price_tail_cap_status", sa.String(length=80)),
    ("risk_governance_drawdown_status", sa.String(length=80)),
    ("risk_governance_approved_before_caps", sa.Boolean()),
    ("risk_governance_approved_after_caps", sa.Boolean()),
    ("risk_governance_shadow_only", sa.Boolean()),
    ("risk_governance_blocked", sa.Boolean()),
    ("risk_governance_rank", sa.Integer()),
    ("risk_governance_rank_score", sa.Numeric(14, 6)),
)


def _set_fast_postgres_timeouts() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("SET lock_timeout = '5s'")
        op.execute("SET statement_timeout = '30s'")


def upgrade() -> None:
    _set_fast_postgres_timeouts()
    for table_name in ("model_candidates", "paper_trades"):
        for name, column_type in RISK_GOVERNANCE_COLUMN_SPECS:
            op.add_column(table_name, sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    _set_fast_postgres_timeouts()
    for table_name in ("paper_trades", "model_candidates"):
        for name, _column_type in reversed(RISK_GOVERNANCE_COLUMN_SPECS):
            op.drop_column(table_name, name)
