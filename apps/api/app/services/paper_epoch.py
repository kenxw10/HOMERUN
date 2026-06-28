from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    BalanceSnapshot,
    JobRun,
    ModelCandidate,
    ModelPredictionOutput,
    ModelPredictionRun,
    PaperTrade,
    PaperTradingEpoch,
)
from app.time_utils import utc_now

PRE_PR3D_EPOCH_KEY = "pre_pr3d_validation"
DEFAULT_ACTIVE_EPOCH_KEY = "pr3d_paper_observation_v1"
RESET_CONFIRMATION = "RESET_PAPER_EPOCH"


@dataclass(frozen=True)
class EpochFilter:
    epoch: PaperTradingEpoch
    include_archived: bool = False


def _display_name(epoch_key: str) -> str:
    return epoch_key.replace("_", " ").upper()


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def _active_epochs(session: Session) -> list[PaperTradingEpoch]:
    return list(
        session.scalars(
            select(PaperTradingEpoch)
            .where(PaperTradingEpoch.mode == "paper")
            .where(PaperTradingEpoch.status == "active")
            .order_by(PaperTradingEpoch.started_at.desc(), PaperTradingEpoch.id.desc())
        )
    )


def get_or_create_active_paper_epoch(
    session: Session,
    *,
    epoch_key: str = DEFAULT_ACTIVE_EPOCH_KEY,
    starting_balance: Decimal | None = None,
) -> PaperTradingEpoch:
    active = _active_epochs(session)
    if active:
        keeper = active[0]
        now = utc_now()
        for duplicate in active[1:]:
            duplicate.status = "archived"
            duplicate.archived_at = duplicate.archived_at or now
            duplicate.archive_reason = duplicate.archive_reason or "duplicate_active_epoch_auto_archived"
            session.add(duplicate)
        return keeper

    settings = get_settings()
    balance = _money(starting_balance or settings.paper_starting_balance)
    existing = session.scalar(select(PaperTradingEpoch).where(PaperTradingEpoch.epoch_key == epoch_key))
    now = utc_now()
    if existing is not None:
        existing.status = "active"
        existing.mode = "paper"
        existing.started_at = existing.started_at or now
        existing.archived_at = None
        existing.archive_reason = None
        existing.starting_balance = existing.starting_balance or balance
        session.add(existing)
        session.flush()
        return existing

    epoch = PaperTradingEpoch(
        epoch_key=epoch_key,
        display_name=_display_name(epoch_key),
        status="active",
        mode="paper",
        starting_balance=balance,
        started_at=now,
        notes={"created_by": "get_or_create_active_paper_epoch"},
    )
    session.add(epoch)
    session.flush()
    return epoch


def create_new_active_epoch(
    session: Session,
    *,
    epoch_key: str,
    starting_balance: Decimal,
    notes: dict[str, object] | None = None,
) -> PaperTradingEpoch:
    now = utc_now()
    for active in _active_epochs(session):
        active.status = "archived"
        active.archived_at = active.archived_at or now
        active.archive_reason = active.archive_reason or "new_active_epoch_created"
        session.add(active)

    existing = session.scalar(select(PaperTradingEpoch).where(PaperTradingEpoch.epoch_key == epoch_key))
    if existing is not None:
        existing.status = "active"
        existing.mode = "paper"
        existing.display_name = _display_name(epoch_key)
        existing.starting_balance = _money(starting_balance)
        existing.started_at = now
        existing.archived_at = None
        existing.archive_reason = None
        existing.notes = notes or existing.notes
        session.add(existing)
        session.flush()
        return existing

    epoch = PaperTradingEpoch(
        epoch_key=epoch_key,
        display_name=_display_name(epoch_key),
        status="active",
        mode="paper",
        starting_balance=_money(starting_balance),
        started_at=now,
        notes=notes or {},
    )
    session.add(epoch)
    session.flush()
    return epoch


def archive_current_epoch(
    session: Session,
    *,
    archive_key: str = PRE_PR3D_EPOCH_KEY,
    reason: str = "paper_epoch_reset",
) -> PaperTradingEpoch:
    now = utc_now()
    archive = session.scalar(select(PaperTradingEpoch).where(PaperTradingEpoch.epoch_key == archive_key))
    if archive is None:
        archive = PaperTradingEpoch(
            epoch_key=archive_key,
            display_name=_display_name(archive_key),
            status="archived",
            mode="paper",
            starting_balance=_money(get_settings().paper_starting_balance),
            started_at=now,
            archived_at=now,
            archive_reason=reason,
            notes={"created_by": "archive_current_epoch"},
        )
        session.add(archive)
        session.flush()
    else:
        archive.status = "archived"
        archive.mode = "paper"
        archive.archived_at = archive.archived_at or now
        archive.archive_reason = archive.archive_reason or reason
        session.add(archive)
        session.flush()

    active_ids = [epoch.id for epoch in _active_epochs(session)]
    source_ids = set(active_ids)
    for epoch_id in active_ids:
        epoch = session.get(PaperTradingEpoch, epoch_id)
        if epoch is not None and epoch.id != archive.id:
            epoch.status = "archived"
            epoch.archived_at = epoch.archived_at or now
            epoch.archive_reason = epoch.archive_reason or reason
            session.add(epoch)

    for model in (BalanceSnapshot, ModelCandidate, PaperTrade, ModelPredictionRun, ModelPredictionOutput, JobRun):
        query = select(model).where(model.paper_trading_epoch_id.is_(None))
        if source_ids:
            query = select(model).where(
                (model.paper_trading_epoch_id.is_(None)) | (model.paper_trading_epoch_id.in_(source_ids))
            )
        for row in session.scalars(query):
            row.paper_trading_epoch_id = archive.id
            session.add(row)

    for trade in session.scalars(
        select(PaperTrade)
        .where(PaperTrade.paper_trading_epoch_id == archive.id)
        .where(PaperTrade.status == "open")
    ):
        trade.status = "archived"
        trade.resolution = trade.resolution or "EPOCH_ARCHIVED"
        trade.exit_time = trade.exit_time or now
        session.add(trade)

    session.flush()
    return archive


def resolve_epoch_filter(
    session: Session,
    *,
    epoch_key: str | None = None,
    include_archived: bool = False,
) -> EpochFilter:
    if epoch_key:
        epoch = session.scalar(select(PaperTradingEpoch).where(PaperTradingEpoch.epoch_key == epoch_key))
        if epoch is None:
            raise ValueError(f"Unknown paper trading epoch: {epoch_key}")
        return EpochFilter(epoch=epoch, include_archived=include_archived)
    return EpochFilter(epoch=get_or_create_active_paper_epoch(session), include_archived=include_archived)


def active_epoch_summary(session: Session) -> dict[str, object]:
    epoch = get_or_create_active_paper_epoch(session)
    return {
        "id": epoch.id,
        "epoch_key": epoch.epoch_key,
        "display_name": epoch.display_name,
        "status": epoch.status,
        "mode": epoch.mode,
        "starting_balance": float(epoch.starting_balance),
        "started_at": epoch.started_at.isoformat() if epoch.started_at else None,
        "archived_at": epoch.archived_at.isoformat() if epoch.archived_at else None,
        "archive_reason": epoch.archive_reason,
        "current_balance_snapshot_id": epoch.current_balance_snapshot_id,
        "notes": epoch.notes or {},
    }


def reset_paper_trading_epoch(
    session: Session,
    *,
    archive_current_as: str,
    new_epoch: str,
    starting_balance: Decimal,
    archive_open_positions: bool,
    reset_dashboard_metrics: bool,
    confirmation: str,
) -> dict[str, object]:
    if confirmation != RESET_CONFIRMATION:
        raise ValueError("Confirmation must be RESET_PAPER_EPOCH.")
    if starting_balance <= Decimal("0"):
        raise ValueError("starting_balance must be greater than zero.")

    archived_epoch = archive_current_epoch(
        session,
        archive_key=archive_current_as or PRE_PR3D_EPOCH_KEY,
        reason="admin_reset_epoch",
    )
    if not archive_open_positions:
        for trade in session.scalars(
            select(PaperTrade)
            .where(PaperTrade.paper_trading_epoch_id == archived_epoch.id)
            .where(PaperTrade.status == "archived")
            .where(PaperTrade.resolution == "EPOCH_ARCHIVED")
        ):
            trade.status = "open"
            trade.resolution = None
            session.add(trade)

    counts = {
        "archived_trades_count": int(
            session.scalar(select(func.count(PaperTrade.id)).where(PaperTrade.paper_trading_epoch_id == archived_epoch.id)) or 0
        ),
        "archived_candidates_count": int(
            session.scalar(
                select(func.count(ModelCandidate.id)).where(ModelCandidate.paper_trading_epoch_id == archived_epoch.id)
            )
            or 0
        ),
        "archived_prediction_runs_count": int(
            session.scalar(
                select(func.count(ModelPredictionRun.id)).where(
                    ModelPredictionRun.paper_trading_epoch_id == archived_epoch.id
                )
            )
            or 0
        ),
        "archived_balance_snapshots_count": int(
            session.scalar(
                select(func.count(BalanceSnapshot.id)).where(BalanceSnapshot.paper_trading_epoch_id == archived_epoch.id)
            )
            or 0
        ),
    }

    active = create_new_active_epoch(
        session,
        epoch_key=new_epoch,
        starting_balance=starting_balance,
        notes={
            "archive_current_as": archive_current_as,
            "archive_open_positions": archive_open_positions,
            "reset_dashboard_metrics": reset_dashboard_metrics,
        },
    )
    snapshot = BalanceSnapshot(
        paper_trading_epoch_id=active.id,
        captured_at=utc_now(),
        cash_balance=_money(starting_balance),
        portfolio_value=_money(starting_balance),
        source="paper",
        snapshot_type="epoch_reset",
    )
    session.add(snapshot)
    session.flush()
    active.current_balance_snapshot_id = snapshot.id
    session.add(active)
    session.commit()

    return {
        "archived_epoch_key": archived_epoch.epoch_key,
        "new_epoch_key": active.epoch_key,
        "starting_balance": float(active.starting_balance),
        **counts,
        "new_balance_snapshot_id": snapshot.id,
        "active_epoch": active_epoch_summary(session),
    }
