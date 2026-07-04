"""PR4a add paper trade settlement and model audit columns.

Revision ID: 0018_pr4a_settlement_audit
Revises: 0017_pr3x_risk_governance
Create Date: 2026-07-04
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0018_pr4a_settlement_audit"
down_revision: str | None = "0017_pr3x_risk_governance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


PAPER_TRADE_AUDIT_COLUMN_SPECS = (
    ("probability_adapter_key", sa.String(length=120)),
    ("probability_adapter_version", sa.String(length=120)),
    ("probability_adapter_policy_version", sa.String(length=120)),
    ("probability_adapter_family", sa.String(length=80)),
    ("probability_adapter_scope", sa.String(length=40)),
    ("probability_adapter_calibration_hook", sa.String(length=120)),
    ("probability_adapter_calibration_version", sa.String(length=120)),
    ("probability_adapter_feature_policy_version", sa.String(length=120)),
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
    ("calibration_status", sa.String(length=80)),
    ("settlement_audit_key", sa.String(length=220)),
    ("settlement_formula_version", sa.String(length=120)),
    ("settlement_formula", sa.Text()),
    ("settlement_source", sa.String(length=80)),
    ("settlement_source_game_id", sa.String(length=120)),
    ("settlement_source_market_ticker", sa.String(length=120)),
    ("settlement_checked_at", sa.DateTime(timezone=True)),
    ("settlement_resolved_at", sa.DateTime(timezone=True)),
    ("settlement_status", sa.String(length=80)),
    ("settlement_outcome", sa.String(length=40)),
    ("settlement_skip_reason", sa.String(length=160)),
    ("settlement_error_reason", sa.String(length=160)),
    ("settlement_idempotency_key", sa.String(length=220)),
    ("settlement_payout", sa.Numeric(14, 2)),
    ("settlement_fee_adjustment", sa.Numeric(14, 2)),
)


def _set_fast_postgres_timeouts() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("SET lock_timeout = '5s'")
        op.execute("SET statement_timeout = '30s'")


def upgrade() -> None:
    _set_fast_postgres_timeouts()
    for name, column_type in PAPER_TRADE_AUDIT_COLUMN_SPECS:
        op.add_column("paper_trades", sa.Column(name, column_type, nullable=True))
    op.create_index(
        "ix_paper_trades_settlement_audit_key",
        "paper_trades",
        ["settlement_audit_key"],
        unique=False,
    )


def downgrade() -> None:
    _set_fast_postgres_timeouts()
    op.drop_index("ix_paper_trades_settlement_audit_key", table_name="paper_trades")
    for name, _column_type in reversed(PAPER_TRADE_AUDIT_COLUMN_SPECS):
        op.drop_column("paper_trades", name)
