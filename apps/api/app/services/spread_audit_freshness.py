from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import JobRun, PaperTrade, PaperTradingEpoch
from app.time_utils import ensure_aware_utc, utc_now

SPREAD_AUDIT_FRESHNESS_POLICY_VERSION = "pr4e_spread_audit_coverage_freshness_v1"
SPREAD_AUDIT_FRESHNESS_MAX_AGE_HOURS = 36
SPREAD_AUDIT_RECENT_ACTIVITY_DAYS = 7
SPREAD_AUDIT_CURRENT_RUN_LIMIT = 25
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


def _int_json(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip('"'))
    except (TypeError, ValueError):
        return None


def _str_json(value: object) -> str | None:
    if value is None:
        return None
    parsed = str(value).strip('"')
    return parsed or None


def _reference_time(row: object) -> datetime | None:
    completed_at = row["completed_at"]  # type: ignore[index]
    started_at = row["started_at"]  # type: ignore[index]
    return completed_at or started_at


def _age_hours(captured_at: datetime, row: object | None) -> float | None:
    if row is None:
        return None
    reference_time = _reference_time(row)
    if reference_time is None:
        return None
    return round((captured_at - ensure_aware_utc(reference_time)).total_seconds() / 3600, 2)


def _spread_audit_rows(
    session: Session,
    epoch: PaperTradingEpoch,
    *,
    target_date: date | None,
    limit: int,
) -> list[object]:
    spread_result = JobRun.result["spread_audit"]
    statement = (
        select(
            JobRun.status.label("job_status"),
            JobRun.started_at,
            JobRun.completed_at,
            JobRun.target_date,
            spread_result["checked"].label("checked"),
            spread_result["coverage_status"].label("coverage_status"),
            spread_result["zero_checked_reason"].label("zero_checked_reason"),
            spread_result["target_date_mapping_count"].label("target_date_mapping_count"),
            spread_result["in_window_mapping_count"].label("in_window_mapping_count"),
        )
        .where(JobRun.job_name == "spread-audit")
        .where(JobRun.paper_trading_epoch_id == epoch.id)
        .order_by(JobRun.started_at.desc(), JobRun.id.desc())
        .limit(limit)
    )
    if target_date is not None:
        statement = statement.where(JobRun.target_date == target_date)
    return list(session.execute(statement).mappings())


def _run_is_covered(row: object) -> bool:
    return row["job_status"] == "succeeded" and _str_json(row["coverage_status"]) == "covered"  # type: ignore[index]


def _run_is_zero_checked(row: object) -> bool:
    return row["job_status"] == "succeeded" and (_int_json(row["checked"]) or 0) == 0  # type: ignore[index]


def _coverage_status_from_successful_row(row: object | None) -> str | None:
    if row is None:
        return None
    return _str_json(row["coverage_status"])  # type: ignore[index]


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
    current_day_rows = _spread_audit_rows(
        session,
        epoch,
        target_date=today_et,
        limit=SPREAD_AUDIT_CURRENT_RUN_LIMIT,
    )
    latest_rows = _spread_audit_rows(session, epoch, target_date=None, limit=1)
    latest_row = latest_rows[0] if latest_rows else None
    current_day_successful_rows = [row for row in current_day_rows if row["job_status"] == "succeeded"]  # type: ignore[index]
    current_day_covered_rows = [row for row in current_day_successful_rows if _run_is_covered(row)]
    current_day_zero_checked_rows = [row for row in current_day_successful_rows if _run_is_zero_checked(row)]
    latest_covered_row = current_day_covered_rows[0] if current_day_covered_rows else None
    latest_successful_current_row = current_day_successful_rows[0] if current_day_successful_rows else None
    latest_evidence_row = latest_covered_row or latest_row
    age_hours = _age_hours(captured_at, latest_evidence_row)
    latest_run_checked = _int_json(latest_row["checked"]) if latest_row is not None else None  # type: ignore[index]
    latest_run_coverage_status = (
        _str_json(latest_row["coverage_status"]) if latest_row is not None else None  # type: ignore[index]
    )
    latest_run_zero_checked_reason = (
        _str_json(latest_row["zero_checked_reason"]) if latest_row is not None else None  # type: ignore[index]
    )
    latest_run_target_date_mapping_count = (
        _int_json(latest_row["target_date_mapping_count"]) if latest_row is not None else None  # type: ignore[index]
    )
    latest_run_in_window_mapping_count = (
        _int_json(latest_row["in_window_mapping_count"]) if latest_row is not None else None  # type: ignore[index]
    )

    if latest_row is None and job_status is not None:
        # Backward-compatible fallback for callers that already selected a legacy row.
        age_hours = (
            round((captured_at - ensure_aware_utc(completed_at or started_at)).total_seconds() / 3600, 2)
            if completed_at or started_at
            else None
        )

    if latest_row is None and job_status is None:
        freshness_status = "not_run"
    elif latest_row is not None and latest_row["job_status"] != "succeeded":  # type: ignore[index]
        freshness_status = "latest_not_succeeded"
    elif age_hours is None:
        freshness_status = "missing_coverage_evidence"
    elif age_hours > SPREAD_AUDIT_FRESHNESS_MAX_AGE_HOURS:
        freshness_status = "stale_age"
    elif current_day_covered_rows:
        freshness_status = "fresh_covered"
    elif latest_row is not None and latest_row["target_date"] != today_et:  # type: ignore[index]
        freshness_status = "stale_target_date"
    elif not current_day_successful_rows:
        freshness_status = "latest_not_succeeded"
    else:
        coverage_status = _coverage_status_from_successful_row(latest_successful_current_row)
        if coverage_status in {"no_target_date_mappings", "no_mappings_in_window"}:
            freshness_status = "fresh_no_eligible_markets"
        elif coverage_status == "zero_checked_with_eligible_mappings":
            freshness_status = "incomplete_zero_checked"
        elif coverage_status == "partial_coverage":
            freshness_status = "incomplete_partial_coverage"
        elif coverage_status == "covered":
            freshness_status = "fresh_covered"
        else:
            freshness_status = "missing_coverage_evidence"

    spread_audit_coverage_warning = freshness_status in {
        "incomplete_zero_checked",
        "incomplete_partial_coverage",
        "latest_not_succeeded",
        "missing_coverage_evidence",
    } or latest_run_coverage_status in {"zero_checked_with_eligible_mappings", "partial_coverage"}
    stale_warning = freshness_status not in {"fresh_covered", "fresh_no_eligible_markets"}
    latest_covered_reference = _reference_time(latest_covered_row) if latest_covered_row is not None else None

    return {
        "freshness_policy_version": SPREAD_AUDIT_FRESHNESS_POLICY_VERSION,
        "freshness_max_age_hours": SPREAD_AUDIT_FRESHNESS_MAX_AGE_HOURS,
        "recent_activity_window_days": SPREAD_AUDIT_RECENT_ACTIVITY_DAYS,
        "today_et": today_et.isoformat(),
        "age_hours": age_hours,
        "freshness_status": freshness_status,
        "current_day_audit_run_count": len(current_day_rows),
        "current_day_successful_run_count": len(current_day_successful_rows),
        "current_day_covered_run_count": len(current_day_covered_rows),
        "current_day_zero_checked_run_count": len(current_day_zero_checked_rows),
        "latest_run_checked": latest_run_checked,
        "latest_run_coverage_status": latest_run_coverage_status,
        "latest_run_zero_checked_reason": latest_run_zero_checked_reason,
        "latest_run_target_date_mapping_count": latest_run_target_date_mapping_count,
        "latest_run_in_window_mapping_count": latest_run_in_window_mapping_count,
        "latest_covered_run_at": ensure_aware_utc(latest_covered_reference).isoformat()
        if latest_covered_reference is not None
        else None,
        "latest_covered_target_date": (
            latest_covered_row["target_date"].isoformat()  # type: ignore[index]
            if latest_covered_row is not None and latest_covered_row["target_date"]  # type: ignore[index]
            else None
        ),
        "spread_audit_coverage_warning": spread_audit_coverage_warning,
        "spread_audit_stale_warning": stale_warning,
        "recent_full_game_spread_activity_count": recent_activity_count,
    }
