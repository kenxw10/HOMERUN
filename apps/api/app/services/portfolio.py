from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import BalanceSnapshot, PaperTrade
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


def calculate_paper_portfolio(session: Session) -> PortfolioTotals:
    settings = get_settings()
    starting_balance = Decimal(settings.paper_starting_balance)
    trades = list(session.scalars(select(PaperTrade)))

    realized = sum(
        (trade.realized_pnl or Decimal("0")) for trade in trades if trade.status != "open"
    ) or Decimal("0")
    open_trades = [trade for trade in trades if trade.status == "open"]
    open_cost = sum(trade.entry_price * Decimal(trade.quantity) for trade in open_trades) or Decimal("0")
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


def create_balance_snapshot(session: Session, source: str = "paper_job") -> BalanceSnapshot:
    totals = calculate_paper_portfolio(session)
    snapshot = BalanceSnapshot(
        captured_at=utc_now(),
        cash_balance=totals.cash_balance,
        portfolio_value=totals.portfolio_value,
        source="paper",
        snapshot_type=source,
    )
    session.add(snapshot)
    session.flush()
    return snapshot
