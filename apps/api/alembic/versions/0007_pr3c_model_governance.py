"""PR3c mature model governance metadata.

Revision ID: 0007_pr3c_model_governance
Revises: 0006_pr3b_family_wiring
Create Date: 2026-06-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_pr3c_model_governance"
down_revision: str | None = "0006_pr3b_family_wiring"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _set_fast_postgres_timeouts() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("SET lock_timeout = '5s'")
        op.execute("SET statement_timeout = '30s'")


def upgrade() -> None:
    _set_fast_postgres_timeouts()

    op.add_column("model_candidates", sa.Column("probability_raw", sa.Numeric(8, 6)))
    op.add_column("model_candidates", sa.Column("probability_calibrated", sa.Numeric(8, 6)))
    op.add_column("model_candidates", sa.Column("training_exclusion_reason", sa.String(length=120)))
    op.add_column("model_candidates", sa.Column("data_quality", sa.Numeric(6, 4)))
    op.add_column("model_candidates", sa.Column("calibration_status", sa.String(length=80)))

    op.add_column("feature_snapshots", sa.Column("feature_version", sa.String(length=80)))
    op.add_column("feature_snapshots", sa.Column("source_statuses", sa.JSON()))

    op.create_table(
        "mlb_feature_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mlb_game_id", sa.Integer(), sa.ForeignKey("mlb_games.id")),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data_quality", sa.Numeric(6, 4)),
        sa.Column("source_statuses", sa.JSON()),
        sa.Column("features", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("mlb_game_id", "target_date", "source", name="uq_mlb_feature_game_date_source"),
    )

    op.create_table(
        "model_prediction_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("target_date", sa.Date()),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="running"),
        sa.Column("model_version_tag", sa.String(length=120)),
        sa.Column("feature_version", sa.String(length=80)),
        sa.Column("candidates_evaluated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("trades_created", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("trade_policy", sa.JSON()),
        sa.Column("summary", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "model_prediction_outputs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("prediction_run_id", sa.Integer(), sa.ForeignKey("model_prediction_runs.id")),
        sa.Column("candidate_id", sa.Integer(), sa.ForeignKey("model_candidates.id")),
        sa.Column("market_family", sa.String(length=80)),
        sa.Column("probability_raw", sa.Numeric(8, 6)),
        sa.Column("probability_calibrated", sa.Numeric(8, 6)),
        sa.Column("fair_value", sa.Numeric(8, 4)),
        sa.Column("data_quality", sa.Numeric(6, 4)),
        sa.Column("calibration_status", sa.String(length=80)),
        sa.Column("trade_rank", sa.Integer()),
        sa.Column("decision_reason", sa.String(length=120)),
        sa.Column("raw_output", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "model_governance_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("details", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    _set_fast_postgres_timeouts()

    op.drop_table("model_governance_events")
    op.drop_table("model_prediction_outputs")
    op.drop_table("model_prediction_runs")
    op.drop_table("mlb_feature_snapshots")

    op.drop_column("feature_snapshots", "source_statuses")
    op.drop_column("feature_snapshots", "feature_version")

    op.drop_column("model_candidates", "calibration_status")
    op.drop_column("model_candidates", "data_quality")
    op.drop_column("model_candidates", "training_exclusion_reason")
    op.drop_column("model_candidates", "probability_calibrated")
    op.drop_column("model_candidates", "probability_raw")
