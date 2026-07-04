"""PR3t add live-like selector metadata columns.

Revision ID: 0014_pr3t_live_like_selector
Revises: 0013_pr3s_exposure_taxonomy
Create Date: 2026-07-04
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0014_pr3t_live_like_selector"
down_revision: str | None = "0013_pr3s_exposure_taxonomy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


SELECTOR_COLUMN_SPECS = (
    ("selector_policy_version", sa.String(length=80)),
    ("selector_mode", sa.String(length=40)),
    ("selector_status", sa.String(length=40)),
    ("selector_decision", sa.String(length=120)),
    ("selector_rejection_reason", sa.String(length=120)),
    ("selector_threshold_profile", sa.String(length=120)),
    ("selector_min_net_ev", sa.Numeric(10, 6)),
    ("selector_min_prob_edge", sa.Numeric(10, 6)),
    ("selector_min_data_quality", sa.Numeric(6, 4)),
    ("selector_line_class_policy", sa.String(length=120)),
    ("selector_concept_cluster_key", sa.String(length=160)),
    ("selector_same_game_concept_cluster_key", sa.String(length=180)),
    ("selector_cluster_rank", sa.Integer()),
    ("selector_cluster_rank_score", sa.Numeric(14, 6)),
    ("selector_selected_from_cluster", sa.Boolean()),
    ("selector_shadow_only", sa.Boolean()),
    ("selector_live_like_eligible_before_cluster", sa.Boolean()),
    ("selector_live_like_eligible_after_cluster", sa.Boolean()),
)


def _set_fast_postgres_timeouts() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("SET lock_timeout = '5s'")
        op.execute("SET statement_timeout = '30s'")


def upgrade() -> None:
    _set_fast_postgres_timeouts()
    for table_name in ("model_candidates", "paper_trades"):
        for name, column_type in SELECTOR_COLUMN_SPECS:
            op.add_column(table_name, sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    _set_fast_postgres_timeouts()
    for table_name in ("paper_trades", "model_candidates"):
        for name, _column_type in reversed(SELECTOR_COLUMN_SPECS):
            op.drop_column(table_name, name)
