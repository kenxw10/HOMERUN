"""PR3d paper operations epochs, jobs, and sizing.

Revision ID: 0010_pr3d_paper_ops
Revises: 0009_pr3c_fix2_features
Create Date: 2026-06-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0010_pr3d_paper_ops"
down_revision: str | None = "0009_pr3c_fix2_features"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


GATE_COLUMNS = (
    "gate_mapping_ok",
    "gate_market_open",
    "gate_game_not_started",
    "gate_price_fresh_executable",
    "gate_data_quality_ok",
    "gate_push_ok",
    "gate_probability_present",
    "gate_gross_ev_positive",
    "gate_fee_present",
    "gate_probability_edge_ok",
    "gate_net_ev_ok",
    "gate_calibration_ok",
    "gate_line_selection_ok",
    "gate_caps_ok",
    "gate_open_position_ok",
    "gate_final_trade_eligible",
    "blocked_by_quality_only",
    "would_pass_ev_if_quality_allowed",
    "would_pass_edge_if_quality_allowed",
    "ev_edge_pass_but_quality_fail",
    "counterfactual_trade_eligible_before_quality",
    "counterfactual_trade_eligible_after_quality",
)

SIZING_COLUMNS = (
    sa.Column("bankroll_at_entry", sa.Numeric(14, 2)),
    sa.Column("risk_pct", sa.Numeric(8, 6)),
    sa.Column("risk_dollars", sa.Numeric(14, 2)),
    sa.Column("estimated_cost_per_contract", sa.Numeric(10, 6)),
    sa.Column("estimated_total_cost", sa.Numeric(14, 2)),
    sa.Column("one_contract_expected_value", sa.Numeric(10, 6)),
    sa.Column("sized_expected_value", sa.Numeric(14, 6)),
    sa.Column("one_contract_fee_estimate", sa.Numeric(10, 6)),
    sa.Column("total_fee_estimate", sa.Numeric(14, 6)),
)


def _set_fast_postgres_timeouts() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("SET lock_timeout = '5s'")
        op.execute("SET statement_timeout = '30s'")


def _seed_archived_epoch() -> None:
    if context.is_offline_mode():
        return

    bind = op.get_bind()
    now = sa.func.now()
    paper_epochs = sa.table(
        "paper_trading_epochs",
        sa.column("id", sa.Integer),
        sa.column("epoch_key", sa.String),
        sa.column("display_name", sa.String),
        sa.column("status", sa.String),
        sa.column("mode", sa.String),
        sa.column("starting_balance", sa.Numeric),
        sa.column("started_at", sa.DateTime(timezone=True)),
        sa.column("archived_at", sa.DateTime(timezone=True)),
        sa.column("archive_reason", sa.Text),
        sa.column("notes", sa.JSON),
    )
    existing = bind.execute(
        sa.select(paper_epochs.c.id).where(paper_epochs.c.epoch_key == "pre_pr3d_validation")
    ).scalar_one_or_none()
    if existing is None:
        bind.execute(
            paper_epochs.insert().values(
                epoch_key="pre_pr3d_validation",
                display_name="PRE PR3D VALIDATION",
                status="archived",
                mode="paper",
                starting_balance="1000.00",
                started_at=now,
                archived_at=now,
                archive_reason="migration_archive_existing_validation_rows",
                notes={"created_by": "0010_pr3d_paper_ops"},
            )
        )
        existing = bind.execute(
            sa.select(paper_epochs.c.id).where(paper_epochs.c.epoch_key == "pre_pr3d_validation")
        ).scalar_one()

    for table_name in (
        "balance_snapshots",
        "model_candidates",
        "paper_trades",
        "model_prediction_runs",
        "model_prediction_outputs",
    ):
        table = sa.table(table_name, sa.column("paper_trading_epoch_id", sa.Integer))
        bind.execute(
            table.update()
            .where(table.c.paper_trading_epoch_id.is_(None))
            .values(paper_trading_epoch_id=existing)
        )


def upgrade() -> None:
    _set_fast_postgres_timeouts()

    op.create_table(
        "paper_trading_epochs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("epoch_key", sa.String(length=120), nullable=False, unique=True),
        sa.Column("display_name", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="active"),
        sa.Column("mode", sa.String(length=40), nullable=False, server_default="paper"),
        sa.Column("starting_balance", sa.Numeric(14, 2), nullable=False),
        sa.Column("current_balance_snapshot_id", sa.Integer()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column("archive_reason", sa.Text()),
        sa.Column("notes", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_paper_trading_epochs_epoch_key", "paper_trading_epochs", ["epoch_key"])
    op.create_index("ix_paper_trading_epochs_status", "paper_trading_epochs", ["status"])

    for table_name in (
        "balance_snapshots",
        "model_candidates",
        "paper_trades",
        "model_prediction_runs",
        "model_prediction_outputs",
    ):
        op.add_column(table_name, sa.Column("paper_trading_epoch_id", sa.Integer()))
        op.create_index(f"ix_{table_name}_paper_trading_epoch_id", table_name, ["paper_trading_epoch_id"])

    op.create_foreign_key(
        "fk_paper_epochs_current_snapshot",
        "paper_trading_epochs",
        "balance_snapshots",
        ["current_balance_snapshot_id"],
        ["id"],
    )
    for table_name in (
        "balance_snapshots",
        "model_candidates",
        "paper_trades",
        "model_prediction_runs",
        "model_prediction_outputs",
    ):
        op.create_foreign_key(
            f"fk_{table_name}_paper_epoch",
            table_name,
            "paper_trading_epochs",
            ["paper_trading_epoch_id"],
            ["id"],
        )

    op.add_column("kalshi_markets", sa.Column("websocket_updated_at", sa.DateTime(timezone=True)))
    op.add_column("kalshi_markets", sa.Column("market_data_source", sa.String(length=40)))

    op.add_column("model_candidates", sa.Column("gate_diagnostics", sa.JSON()))
    for column_name in GATE_COLUMNS:
        op.add_column("model_candidates", sa.Column(column_name, sa.Boolean()))
    op.add_column("model_candidates", sa.Column("contracts", sa.Integer()))
    for column in SIZING_COLUMNS:
        op.add_column("model_candidates", column.copy())

    for column in SIZING_COLUMNS:
        op.add_column("paper_trades", column.copy())

    op.create_table(
        "job_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_name", sa.String(length=120), nullable=False),
        sa.Column("job_type", sa.String(length=80), nullable=False),
        sa.Column("target_date", sa.Date()),
        sa.Column("paper_trading_epoch_id", sa.Integer(), sa.ForeignKey("paper_trading_epochs.id")),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("duration_seconds", sa.Integer()),
        sa.Column("lock_key", sa.String(length=180)),
        sa.Column("triggered_by", sa.String(length=40), nullable=False, server_default="manual"),
        sa.Column("steps", sa.JSON()),
        sa.Column("result", sa.JSON()),
        sa.Column("warnings", sa.JSON()),
        sa.Column("errors", sa.JSON()),
        sa.Column("idempotency_key", sa.String(length=180)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_job_runs_name_date_status", "job_runs", ["job_name", "target_date", "status"])
    op.create_index("ix_job_runs_lock_status", "job_runs", ["lock_key", "status"])
    op.create_index("ix_job_runs_started_at", "job_runs", ["started_at"])
    op.create_index("ix_job_runs_epoch", "job_runs", ["paper_trading_epoch_id"])

    op.create_table(
        "market_data_worker_status",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("status_key", sa.String(length=80), nullable=False, unique=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("running", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source", sa.String(length=40), nullable=False, server_default="rest_fallback"),
        sa.Column("subscribed_market_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("last_message_at", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("reconnect_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stale_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text()),
        sa.Column("raw_status", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    _seed_archived_epoch()


def downgrade() -> None:
    _set_fast_postgres_timeouts()

    op.drop_table("market_data_worker_status")
    op.drop_index("ix_job_runs_epoch", table_name="job_runs")
    op.drop_index("ix_job_runs_started_at", table_name="job_runs")
    op.drop_index("ix_job_runs_lock_status", table_name="job_runs")
    op.drop_index("ix_job_runs_name_date_status", table_name="job_runs")
    op.drop_table("job_runs")

    for column in reversed(SIZING_COLUMNS):
        op.drop_column("paper_trades", column.name)
    for column in reversed(SIZING_COLUMNS):
        op.drop_column("model_candidates", column.name)
    op.drop_column("model_candidates", "contracts")
    for column_name in reversed(GATE_COLUMNS):
        op.drop_column("model_candidates", column_name)
    op.drop_column("model_candidates", "gate_diagnostics")

    op.drop_column("kalshi_markets", "market_data_source")
    op.drop_column("kalshi_markets", "websocket_updated_at")

    for table_name in (
        "model_prediction_outputs",
        "model_prediction_runs",
        "paper_trades",
        "model_candidates",
        "balance_snapshots",
    ):
        op.drop_constraint(f"fk_{table_name}_paper_epoch", table_name, type_="foreignkey")
        op.drop_index(f"ix_{table_name}_paper_trading_epoch_id", table_name=table_name)
        op.drop_column(table_name, "paper_trading_epoch_id")

    op.drop_constraint("fk_paper_epochs_current_snapshot", "paper_trading_epochs", type_="foreignkey")
    op.drop_index("ix_paper_trading_epochs_status", table_name="paper_trading_epochs")
    op.drop_index("ix_paper_trading_epochs_epoch_key", table_name="paper_trading_epochs")
    op.drop_table("paper_trading_epochs")
