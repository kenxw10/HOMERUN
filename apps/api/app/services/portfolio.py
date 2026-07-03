from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import BalanceSnapshot, PaperTrade, PaperTradingEpoch
from app.services.paper_epoch import get_or_create_active_paper_epoch
from app.time_utils import utc_now


@dataclass(frozen=True)
class PortfolioTotals:
    cash_balance: Decimal
    portfolio_value: Decimal
    open_cost: Decimal
    open_mark_value: Decimal
    realized_pnl: Decimal


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def _latest_balance_snapshot(session: Session, epoch: PaperTradingEpoch) -> BalanceSnapshot | None:
    return session.scalar(
        select(BalanceSnapshot)
        .where(BalanceSnapshot.paper_trading_epoch_id == epoch.id)
        .order_by(BalanceSnapshot.captured_at.desc(), BalanceSnapshot.id.desc())
        .limit(1)
    )


def _snapshot_values_changed(snapshot: BalanceSnapshot | None, totals: PortfolioTotals) -> bool:
    if snapshot is None:
        return True
    return snapshot.cash_balance != totals.cash_balance or snapshot.portfolio_value != totals.portfolio_value


def paper_trade_fee(trade: PaperTrade) -> Decimal:
    value = trade.fee_paid if trade.fee_paid is not None else trade.total_fee_estimate
    return _money(Decimal(value or "0"))


def calculate_paper_portfolio(
    session: Session,
    *,
    epoch: PaperTradingEpoch | None = None,
    include_archived: bool = False,
) -> PortfolioTotals:
    settings = get_settings()
    active_epoch = epoch or get_or_create_active_paper_epoch(session)
    starting_balance = Decimal(active_epoch.starting_balance or settings.paper_starting_balance)
    query = select(PaperTrade)
    if not include_archived:
        query = query.where(PaperTrade.paper_trading_epoch_id == active_epoch.id)
    trades = list(session.scalars(query))

    realized = sum(
        (trade.realized_pnl or Decimal("0")) for trade in trades if trade.status != "open"
    ) or Decimal("0")
    open_trades = [trade for trade in trades if trade.status == "open"]
    open_cost = sum(
        (trade.entry_price * Decimal(trade.quantity)) + paper_trade_fee(trade) for trade in open_trades
    ) or Decimal("0")
    open_mark = sum(
        (trade.current_price if trade.current_price is not None else trade.entry_price) * Decimal(trade.quantity)
        for trade in open_trades
    ) or Decimal("0")
    cash = starting_balance + realized - open_cost
    portfolio = cash + open_mark
    return PortfolioTotals(
        cash_balance=_money(cash),
        portfolio_value=_money(portfolio),
        open_cost=_money(open_cost),
        open_mark_value=_money(open_mark),
        realized_pnl=_money(realized),
    )


def create_balance_snapshot(
    session: Session,
    source: str = "paper_job",
    *,
    epoch: PaperTradingEpoch | None = None,
    include_archived: bool = False,
) -> BalanceSnapshot:
    active_epoch = epoch or get_or_create_active_paper_epoch(session)
    totals = calculate_paper_portfolio(session, epoch=active_epoch, include_archived=include_archived)
    latest = _latest_balance_snapshot(session, active_epoch)
    if latest is not None and not _snapshot_values_changed(latest, totals):
        active_epoch.current_balance_snapshot_id = latest.id
        session.add(active_epoch)
        session.flush()
        return latest
    snapshot = BalanceSnapshot(
        paper_trading_epoch_id=active_epoch.id,
        captured_at=utc_now(),
        cash_balance=totals.cash_balance,
        portfolio_value=totals.portfolio_value,
        source="paper",
        snapshot_type=source,
    )
    session.add(snapshot)
    session.flush()
    active_epoch.current_balance_snapshot_id = snapshot.id
    session.add(active_epoch)
    return snapshot
