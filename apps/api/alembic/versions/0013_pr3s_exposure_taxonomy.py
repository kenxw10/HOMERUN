"""PR3s add exposure taxonomy metadata columns.

Revision ID: 0013_pr3s_exposure_taxonomy
Revises: 0012_pr3p2_status_query_indexes
Create Date: 2026-07-03
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0013_pr3s_exposure_taxonomy"
down_revision: str | None = "0012_pr3p2_status_query_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


EXPOSURE_COLUMN_SPECS = (
    ("economic_exposure_label", sa.Text()),
    ("economic_exposure_key", sa.String(length=180)),
    ("economic_exposure_family", sa.String(length=40)),
    ("economic_exposure_scope", sa.String(length=40)),
    ("economic_exposure_direction", sa.String(length=40)),
    ("economic_exposure_team", sa.String(length=40)),
    ("economic_exposure_line", sa.Numeric(10, 4)),
    ("contract_mechanics_label", sa.Text()),
    ("concept_cluster_key", sa.String(length=160)),
    ("same_game_concept_cluster_key", sa.String(length=180)),
    ("line_class", sa.String(length=40)),
    ("line_class_reason", sa.String(length=120)),
    ("line_ladder_rank", sa.Integer()),
    ("line_ladder_distance_from_central", sa.Integer()),
    ("line_ladder_size", sa.Integer()),
    ("exposure_taxonomy_version", sa.String(length=80)),
    ("line_classification_policy_version", sa.String(length=80)),
)


def _set_fast_postgres_timeouts() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("SET lock_timeout = '5s'")
        op.execute("SET statement_timeout = '30s'")


def upgrade() -> None:
    _set_fast_postgres_timeouts()
    for table_name in ("model_candidates", "paper_trades"):
        for name, column_type in EXPOSURE_COLUMN_SPECS:
            op.add_column(table_name, sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    _set_fast_postgres_timeouts()
    for table_name in ("paper_trades", "model_candidates"):
        for name, _column_type in reversed(EXPOSURE_COLUMN_SPECS):
            op.drop_column(table_name, name)
