"""PR3c fix2 feature cache and parameter versions.

Revision ID: 0009_pr3c_fix2_features
Revises: 0008_pr3c_fee_date_scope
Create Date: 2026-06-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_pr3c_fix2_features"
down_revision: str | None = "0008_pr3c_fee_date_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _set_fast_postgres_timeouts() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("SET lock_timeout = '5s'")
        op.execute("SET statement_timeout = '30s'")


def _module_columns(*extra: sa.Column) -> list[sa.Column]:
    return [
        sa.Column("id", sa.Integer(), primary_key=True),
        *extra,
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("source_status", sa.String(length=40), nullable=False),
        sa.Column("confidence", sa.Numeric(6, 4)),
        sa.Column("completeness", sa.Numeric(6, 4)),
        sa.Column("stale", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("features", sa.JSON(), nullable=False),
        sa.Column("raw_payload", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    _set_fast_postgres_timeouts()

    op.create_table(
        "team_daily_features",
        *_module_columns(
            sa.Column("target_date", sa.Date(), nullable=False),
            sa.Column("team_code", sa.String(length=12), nullable=False),
        ),
        sa.UniqueConstraint("target_date", "team_code", "source", name="uq_team_daily_date_team_source"),
    )
    op.create_table(
        "team_recent_features",
        *_module_columns(
            sa.Column("target_date", sa.Date(), nullable=False),
            sa.Column("team_code", sa.String(length=12), nullable=False),
            sa.Column("window_days", sa.Integer(), nullable=False),
            sa.Column("sample_size", sa.Integer()),
        ),
        sa.UniqueConstraint(
            "target_date",
            "team_code",
            "window_days",
            "source",
            name="uq_team_recent_date_team_window_source",
        ),
    )
    op.create_table(
        "pitcher_daily_features",
        *_module_columns(
            sa.Column("target_date", sa.Date(), nullable=False),
            sa.Column("team_code", sa.String(length=12), nullable=False),
            sa.Column("pitcher_id", sa.String(length=40), nullable=False),
            sa.Column("pitcher_name", sa.String(length=120)),
            sa.Column("sample_size", sa.Integer()),
        ),
        sa.UniqueConstraint(
            "target_date",
            "team_code",
            "pitcher_id",
            "source",
            name="uq_pitcher_daily_date_team_player_source",
        ),
    )
    op.create_table(
        "bullpen_daily_features",
        *_module_columns(
            sa.Column("target_date", sa.Date(), nullable=False),
            sa.Column("team_code", sa.String(length=12), nullable=False),
        ),
        sa.UniqueConstraint("target_date", "team_code", "source", name="uq_bullpen_daily_date_team_source"),
    )
    op.create_table(
        "lineup_snapshots",
        *_module_columns(
            sa.Column("mlb_game_id", sa.Integer(), sa.ForeignKey("mlb_games.id")),
            sa.Column("target_date", sa.Date(), nullable=False),
            sa.Column("team_code", sa.String(length=12), nullable=False),
            sa.Column("lineup_posted_at", sa.DateTime(timezone=True)),
            sa.Column("confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
        ),
        sa.UniqueConstraint("mlb_game_id", "team_code", "source", name="uq_lineup_game_team_source"),
    )
    op.create_table(
        "injury_snapshots",
        *_module_columns(
            sa.Column("target_date", sa.Date(), nullable=False),
            sa.Column("team_code", sa.String(length=12), nullable=False),
        ),
        sa.UniqueConstraint("target_date", "team_code", "source", name="uq_injury_date_team_source"),
    )
    op.create_table(
        "weather_snapshots",
        *_module_columns(
            sa.Column("mlb_game_id", sa.Integer(), sa.ForeignKey("mlb_games.id")),
            sa.Column("target_date", sa.Date(), nullable=False),
            sa.Column("venue_name", sa.String(length=120)),
            sa.Column("forecast_time", sa.DateTime(timezone=True)),
        ),
        sa.UniqueConstraint("mlb_game_id", "source", name="uq_weather_game_source"),
    )
    op.create_table(
        "park_factor_snapshots",
        *_module_columns(sa.Column("venue_name", sa.String(length=120), nullable=False)),
        sa.UniqueConstraint("venue_name", "source", name="uq_park_factor_venue_source"),
    )
    op.create_table(
        "travel_schedule_features",
        *_module_columns(
            sa.Column("mlb_game_id", sa.Integer(), sa.ForeignKey("mlb_games.id")),
            sa.Column("target_date", sa.Date(), nullable=False),
            sa.Column("team_code", sa.String(length=12), nullable=False),
        ),
        sa.UniqueConstraint("mlb_game_id", "team_code", "source", name="uq_travel_game_team_source"),
    )

    op.create_table(
        "model_parameter_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version_tag", sa.String(length=120), nullable=False, unique=True),
        sa.Column("model_family", sa.String(length=80), nullable=False),
        sa.Column("role", sa.String(length=40), nullable=False, server_default="challenger"),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="created"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_reason", sa.Text()),
        sa.Column("trained_at", sa.DateTime(timezone=True)),
        sa.Column("promoted_at", sa.DateTime(timezone=True)),
        sa.Column("source_training_run_id", sa.Integer(), sa.ForeignKey("training_runs.id")),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("metrics", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "model_training_datasets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("training_run_id", sa.Integer(), sa.ForeignKey("training_runs.id")),
        sa.Column("created_at_snapshot", sa.DateTime(timezone=True), nullable=False),
        sa.Column("feature_version", sa.String(length=80), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("split_policy", sa.String(length=80), nullable=False),
        sa.Column("filters", sa.JSON()),
        sa.Column("candidate_ids", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "model_threshold_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version_tag", sa.String(length=120), nullable=False, unique=True),
        sa.Column("role", sa.String(length=40), nullable=False, server_default="evaluation"),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="recorded"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at_snapshot", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_training_run_id", sa.Integer(), sa.ForeignKey("training_runs.id")),
        sa.Column("thresholds", sa.JSON(), nullable=False),
        sa.Column("metrics", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    _set_fast_postgres_timeouts()

    op.drop_table("model_threshold_versions")
    op.drop_table("model_training_datasets")
    op.drop_table("model_parameter_versions")
    op.drop_table("travel_schedule_features")
    op.drop_table("park_factor_snapshots")
    op.drop_table("weather_snapshots")
    op.drop_table("injury_snapshots")
    op.drop_table("lineup_snapshots")
    op.drop_table("bullpen_daily_features")
    op.drop_table("pitcher_daily_features")
    op.drop_table("team_recent_features")
    op.drop_table("team_daily_features")
