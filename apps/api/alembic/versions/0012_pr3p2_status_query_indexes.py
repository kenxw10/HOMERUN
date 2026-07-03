"""PR3p.2 add compact status query indexes.

Revision ID: 0012_pr3p2_status_query_indexes
Revises: 0011_pr3i_decision_length
Create Date: 2026-07-03
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012_pr3p2_status_query_indexes"
down_revision: str | None = "0011_pr3i_decision_length"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _set_fast_postgres_timeouts() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("SET lock_timeout = '5s'")
        op.execute("SET statement_timeout = '30s'")


def upgrade() -> None:
    _set_fast_postgres_timeouts()

    op.create_index(
        "ix_model_candidates_epoch_governance_counts",
        "model_candidates",
        [
            "paper_trading_epoch_id",
            "training_eligible",
            "feature_version",
            "outcome",
            "price_status",
            "market_family",
            "target_date",
            "evaluated_at",
        ],
    )
    op.create_index(
        "ix_model_candidates_epoch_decision_scope",
        "model_candidates",
        [
            "paper_trading_epoch_id",
            "evaluated_at",
            "market_family",
            "market_type",
            "inning_scope",
            "decision",
        ],
    )
    op.create_index(
        "ix_mlb_feature_snapshots_date_source_captured",
        "mlb_feature_snapshots",
        ["target_date", "source", "captured_at"],
    )
    op.create_index(
        "ix_balance_snapshots_epoch_captured",
        "balance_snapshots",
        ["paper_trading_epoch_id", "captured_at"],
    )
    op.create_index(
        "ix_job_runs_epoch_name_started_id",
        "job_runs",
        ["paper_trading_epoch_id", "job_name", "started_at", "id"],
    )


def downgrade() -> None:
    _set_fast_postgres_timeouts()

    op.drop_index("ix_job_runs_epoch_name_started_id", table_name="job_runs")
    op.drop_index("ix_balance_snapshots_epoch_captured", table_name="balance_snapshots")
    op.drop_index("ix_mlb_feature_snapshots_date_source_captured", table_name="mlb_feature_snapshots")
    op.drop_index("ix_model_candidates_epoch_decision_scope", table_name="model_candidates")
    op.drop_index("ix_model_candidates_epoch_governance_counts", table_name="model_candidates")
