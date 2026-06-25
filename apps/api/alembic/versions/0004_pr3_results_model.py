"""PR3 paper results, labels, and model governance fields.

Revision ID: 0004_pr3_results_model
Revises: 0003_pr2_5_resolver
Create Date: 2026-06-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_pr3_results_model"
down_revision: str | None = "0003_pr2_5_resolver"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _set_fast_postgres_timeouts() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("SET lock_timeout = '5s'")
        op.execute("SET statement_timeout = '30s'")


def upgrade() -> None:
    _set_fast_postgres_timeouts()

    op.add_column("balance_snapshots", sa.Column("snapshot_type", sa.String(length=40)))

    op.add_column("model_versions", sa.Column("model_family", sa.String(length=80)))
    op.add_column("model_versions", sa.Column("feature_version", sa.String(length=80)))
    op.add_column("model_versions", sa.Column("role", sa.String(length=40)))
    op.add_column("model_versions", sa.Column("promoted_at", sa.DateTime(timezone=True)))

    op.add_column("model_candidates", sa.Column("outcome_source", sa.String(length=80)))
    op.add_column("model_candidates", sa.Column("resolved_at", sa.DateTime(timezone=True)))
    op.add_column("model_candidates", sa.Column("model_version_tag", sa.String(length=120)))
    op.add_column("model_candidates", sa.Column("scoring_rationale", sa.JSON()))
    op.add_column("model_candidates", sa.Column("market_display", sa.Text()))
    op.add_column("model_candidates", sa.Column("selection_display", sa.String(length=40)))
    op.add_column("model_candidates", sa.Column("matchup_display", sa.String(length=80)))
    op.add_column("model_candidates", sa.Column("contract_display", sa.Text()))

    op.add_column("paper_trades", sa.Column("resolution", sa.String(length=40)))
    op.add_column("paper_trades", sa.Column("fee_paid", sa.Numeric(14, 2)))
    op.add_column("paper_trades", sa.Column("settled_at", sa.DateTime(timezone=True)))
    op.add_column("paper_trades", sa.Column("outcome", sa.String(length=40)))
    op.add_column("paper_trades", sa.Column("market_display", sa.Text()))
    op.add_column("paper_trades", sa.Column("selection_display", sa.String(length=40)))
    op.add_column("paper_trades", sa.Column("matchup_display", sa.String(length=80)))
    op.add_column("paper_trades", sa.Column("contract_display", sa.Text()))

    op.add_column("settlements", sa.Column("paper_trade_id", sa.Integer()))
    op.add_column("settlements", sa.Column("outcome", sa.String(length=40)))
    op.add_column("settlements", sa.Column("fee_paid", sa.Numeric(14, 2)))
    op.create_foreign_key(
        "fk_settlements_paper_trade_id_paper_trades",
        "settlements",
        "paper_trades",
        ["paper_trade_id"],
        ["id"],
    )
    op.create_unique_constraint("uq_settlement_paper_trade", "settlements", ["paper_trade_id"])
    op.alter_column("settlements", "position_id", existing_type=sa.Integer(), nullable=True)

    op.create_index("ix_paper_trades_status", "paper_trades", ["status"])
    op.create_index("ix_model_candidates_outcome", "model_candidates", ["outcome"])


def downgrade() -> None:
    _set_fast_postgres_timeouts()

    op.drop_index("ix_model_candidates_outcome", table_name="model_candidates")
    op.drop_index("ix_paper_trades_status", table_name="paper_trades")

    op.alter_column("settlements", "position_id", existing_type=sa.Integer(), nullable=False)
    op.drop_constraint("uq_settlement_paper_trade", "settlements", type_="unique")
    op.drop_constraint("fk_settlements_paper_trade_id_paper_trades", "settlements", type_="foreignkey")
    op.drop_column("settlements", "fee_paid")
    op.drop_column("settlements", "outcome")
    op.drop_column("settlements", "paper_trade_id")

    op.drop_column("paper_trades", "contract_display")
    op.drop_column("paper_trades", "matchup_display")
    op.drop_column("paper_trades", "selection_display")
    op.drop_column("paper_trades", "market_display")
    op.drop_column("paper_trades", "outcome")
    op.drop_column("paper_trades", "settled_at")
    op.drop_column("paper_trades", "fee_paid")
    op.drop_column("paper_trades", "resolution")

    op.drop_column("model_candidates", "contract_display")
    op.drop_column("model_candidates", "matchup_display")
    op.drop_column("model_candidates", "selection_display")
    op.drop_column("model_candidates", "market_display")
    op.drop_column("model_candidates", "scoring_rationale")
    op.drop_column("model_candidates", "model_version_tag")
    op.drop_column("model_candidates", "resolved_at")
    op.drop_column("model_candidates", "outcome_source")

    op.drop_column("model_versions", "promoted_at")
    op.drop_column("model_versions", "role")
    op.drop_column("model_versions", "feature_version")
    op.drop_column("model_versions", "model_family")

    op.drop_column("balance_snapshots", "snapshot_type")
