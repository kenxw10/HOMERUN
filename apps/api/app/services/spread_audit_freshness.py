from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import PaperTrade, PaperTradingEpoch
from app.time_utils import ensure_aware_utc, utc_now

SPREAD_AUDIT_FRESHNESS_POLICY_VERSION = "pr4d_spread_audit_freshness_v1"
SPREAD_AUDIT_FRESHNESS_MAX_AGE_HOURS = 36
SPREAD_AUDIT_RECENT_ACTIVITY_DAYS = 7
SPREAD_AUDIT_TIME_ZONE = ZoneInfo("America/New_York")


def _recent_full_game_spread_activity_count(
    session: Session,
    epoch: PaperTradingEpoch,
    *,
    now: datetime | None = None,
) -> int:
    captured_at = ensure_aware_utc(now or utc_now())
    cutoff = captured_at - timedelta(days=SPREAD_AUDIT_RECENT_ACTIVITY_DAYS)
    return int(
        session.scalar(
            select(func.count(PaperTrade.id))
            .where(PaperTrade.paper_trading_epoch_id == epoch.id)
            .where(PaperTrade.market_family == "full_game_spread")
            .where(
                or_(
                    PaperTrade.status == "open",
                    PaperTrade.entry_time >= cutoff,
                    PaperTrade.exit_time >= cutoff,
                    PaperTrade.settled_at >= cutoff,
                    PaperTrade.current_price_updated_at >= cutoff,
                )
            )
        )
        or 0
    )


def spread_audit_freshness_payload(
    session: Session,
    epoch: PaperTradingEpoch,
    *,
    job_status: str | None,
    started_at: datetime | None,
    completed_at: datetime | None,
    target_date: date | None,
    now: datetime | None = None,
) -> dict[str, object]:
    captured_at = ensure_aware_utc(now or utc_now())
    today_et = captured_at.astimezone(SPREAD_AUDIT_TIME_ZONE).date()
    recent_activity_count = _recent_full_game_spread_activity_count(session, epoch, now=captured_at)
    reference_time = completed_at or started_at
    age_hours = (
        round((captured_at - ensure_aware_utc(reference_time)).total_seconds() / 3600, 2)
        if reference_time is not None
        else None
    )

    if job_status is None:
        freshness_status = "not_run"
        stale_warning = True
    elif job_status != "succeeded":
        freshness_status = "latest_not_succeeded"
        stale_warning = True
    elif target_date is None:
        freshness_status = "missing_target_date"
        stale_warning = True
    elif age_hours is None or age_hours > SPREAD_AUDIT_FRESHNESS_MAX_AGE_HOURS:
        freshness_status = "stale_age"
        stale_warning = True
    elif target_date != today_et and recent_activity_count > 0:
        freshness_status = "stale_target_date_with_recent_spread_activity"
        stale_warning = True
    elif target_date != today_et:
        freshness_status = "not_today_no_recent_spread_activity"
        stale_warning = False
    else:
        freshness_status = "fresh"
        stale_warning = False

    return {
        "freshness_policy_version": SPREAD_AUDIT_FRESHNESS_POLICY_VERSION,
        "freshness_max_age_hours": SPREAD_AUDIT_FRESHNESS_MAX_AGE_HOURS,
        "recent_activity_window_days": SPREAD_AUDIT_RECENT_ACTIVITY_DAYS,
        "today_et": today_et.isoformat(),
        "age_hours": age_hours,
        "freshness_status": freshness_status,
        "spread_audit_stale_warning": stale_warning,
        "recent_full_game_spread_activity_count": recent_activity_count,
    }
