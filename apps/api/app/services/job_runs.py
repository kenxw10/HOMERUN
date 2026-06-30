from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import JobRun
from app.services.candidates import generate_candidates
from app.services.features import feature_coverage, sync_mlb_features
from app.services.market_family_discovery import run_market_family_discovery
from app.services.market_family_mapping import sync_market_family_mappings
from app.services.mlb import sync_results, sync_schedule
from app.services.modeling import run_model_governance
from app.services.paper_epoch import get_or_create_active_paper_epoch
from app.services.portfolio import create_balance_snapshot
from app.services.position_refresh import refresh_open_position_prices
from app.services.settlement import settle_paper_trades
from app.time_utils import ensure_aware_utc, today_eastern, utc_now

JOB_NAMES = {
    "daily-setup",
    "candidate-sweep",
    "price-refresh",
    "settlement",
    "governance",
    "full-paper-cycle",
}
DATE_INSENSITIVE_LOCK_JOBS = {"price-refresh"}
TODAY_DEFAULT_LOCK_JOBS = {"daily-setup", "candidate-sweep", "settlement", "full-paper-cycle"}


def _job_lock_key(job_name: str, target_date: date | None) -> str:
    if job_name in DATE_INSENSITIVE_LOCK_JOBS:
        return f"{job_name}:global"
    return f"{job_name}:{target_date.isoformat() if target_date else 'none'}"


def _effective_job_target_date(job_name: str, target_date: date | None) -> date | None:
    if target_date is not None or job_name not in TODAY_DEFAULT_LOCK_JOBS:
        return target_date
    return today_eastern()


def resolve_job_target_date(value: str | date | None) -> date | None:
    if isinstance(value, date):
        return value
    if value is None or value == "":
        return None
    lowered = value.strip().lower()
    if lowered == "today_et":
        return today_eastern()
    if lowered == "yesterday_et":
        return today_eastern() - timedelta(days=1)
    return date.fromisoformat(value)


def _validate_candidate_sweep_options(
    min_time_to_start_minutes: int | None,
    max_time_to_start_minutes: int | None,
) -> None:
    if min_time_to_start_minutes is not None and min_time_to_start_minutes < 0:
        raise ValueError("min_time_to_start_minutes must be greater than or equal to 0.")
    if max_time_to_start_minutes is not None and max_time_to_start_minutes < 0:
        raise ValueError("max_time_to_start_minutes must be greater than or equal to 0.")
    if (
        min_time_to_start_minutes is not None
        and max_time_to_start_minutes is not None
        and min_time_to_start_minutes > max_time_to_start_minutes
    ):
        raise ValueError("min_time_to_start_minutes must be less than or equal to max_time_to_start_minutes.")


def _candidate_sweep_options_enabled(
    min_time_to_start_minutes: int | None,
    max_time_to_start_minutes: int | None,
    sweep_label: str | None,
    dry_run_candidates_only: bool,
) -> bool:
    return (
        min_time_to_start_minutes is not None
        or max_time_to_start_minutes is not None
        or bool(sweep_label)
        or dry_run_candidates_only
    )


def _candidate_sweep_window_result(candidate_result: dict[str, object]) -> dict[str, object]:
    keys = (
        "sweep_label",
        "sweep_window_enabled",
        "min_time_to_start_minutes",
        "max_time_to_start_minutes",
        "dry_run_candidates_only",
        "sweep_started_at",
        "games_total_for_date",
        "games_in_window",
        "games_excluded_too_soon",
        "games_excluded_too_late",
        "games_excluded_started",
        "games_excluded_wrong_date",
        "candidates_in_window",
        "paper_trades_in_window",
        "next_game_in_window_start_time_et",
        "next_excluded_too_late_start_time_et",
        "status",
    )
    return {key: candidate_result.get(key) for key in keys if key in candidate_result}


def _duration_seconds(started_at) -> int:
    return max(0, int((utc_now() - ensure_aware_utc(started_at)).total_seconds()))


def mark_stale_running_jobs(session: Session, *, max_runtime_minutes: int = 60) -> int:
    cutoff = utc_now() - timedelta(minutes=max_runtime_minutes)
    stale_runs = list(
        session.scalars(
            select(JobRun)
            .where(JobRun.status == "running")
            .where(JobRun.started_at < cutoff)
            .order_by(JobRun.started_at.asc())
        )
    )
    for run in stale_runs:
        run.status = "failed_stale"
        run.completed_at = utc_now()
        run.duration_seconds = _duration_seconds(run.started_at)
        run.errors = [*list(run.errors or []), {"message": "Job exceeded max runtime and was marked stale."}]
        session.add(run)
    session.flush()
    return len(stale_runs)


def acquire_job_lock(
    session: Session,
    *,
    job_name: str,
    target_date: date | None,
    triggered_by: str,
    max_runtime_minutes: int = 60,
) -> tuple[JobRun, bool]:
    if job_name not in JOB_NAMES:
        raise ValueError(f"Unknown job: {job_name}")
    epoch = get_or_create_active_paper_epoch(session)
    target_date = _effective_job_target_date(job_name, target_date)
    mark_stale_running_jobs(session, max_runtime_minutes=max_runtime_minutes)
    lock_key = _job_lock_key(job_name, target_date)

    def skipped_for_existing(existing: JobRun) -> tuple[JobRun, bool]:
        skipped = JobRun(
            job_name=job_name,
            job_type="paper_ops",
            target_date=target_date,
            paper_trading_epoch_id=epoch.id,
            status="skipped",
            started_at=utc_now(),
            completed_at=utc_now(),
            heartbeat_at=utc_now(),
            duration_seconds=0,
            lock_key=lock_key,
            triggered_by=triggered_by,
            result={"skipped_reason": "skipped_existing_run", "existing_run_id": existing.id},
            steps=[],
            errors=[],
            warnings=[],
            idempotency_key=lock_key,
        )
        session.add(skipped)
        session.flush()
        return skipped, False

    existing = session.scalar(
        select(JobRun)
        .where(JobRun.lock_key == lock_key)
        .where(JobRun.status == "running")
        .order_by(JobRun.started_at.desc(), JobRun.id.desc())
        .limit(1)
    )
    if existing is not None:
        return skipped_for_existing(existing)

    run = JobRun(
        job_name=job_name,
        job_type="paper_ops",
        target_date=target_date,
        paper_trading_epoch_id=epoch.id,
        status="running",
        started_at=utc_now(),
        heartbeat_at=utc_now(),
        lock_key=lock_key,
        triggered_by=triggered_by,
        steps=[],
        result={},
        errors=[],
        warnings=[],
        idempotency_key=lock_key,
    )
    session.add(run)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        epoch = get_or_create_active_paper_epoch(session)
        existing = session.scalar(
            select(JobRun)
            .where(JobRun.lock_key == lock_key)
            .where(JobRun.status == "running")
            .order_by(JobRun.started_at.desc(), JobRun.id.desc())
            .limit(1)
        )
        if existing is None:
            raise
        return skipped_for_existing(existing)
    return run, True


def _run_step(run: JobRun, name: str, fn: Callable[[], Any]) -> Any:
    started_at = utc_now()
    step: dict[str, object] = {"name": name, "status": "running", "started_at": started_at.isoformat()}
    steps = [*list(run.steps or []), step]
    run.steps = steps
    try:
        result = fn()
    except Exception as exc:
        step["status"] = "failed"
        step["completed_at"] = utc_now().isoformat()
        step["error"] = {"message": str(exc), "type": exc.__class__.__name__}
        run.steps = steps
        raise
    step["status"] = "succeeded"
    step["completed_at"] = utc_now().isoformat()
    step["result"] = result if isinstance(result, dict) else {"value": result}
    run.steps = steps
    return result


def _daily_setup_steps(session: Session, target_date: date) -> dict[str, object]:
    yesterday = target_date - timedelta(days=1)
    return {
        "schedule": _run_inline(lambda: {"games": sync_schedule(session, target_date)}),
        "results_yesterday": _run_inline(lambda: sync_results(session, yesterday)),
        "results_today": _run_inline(lambda: sync_results(session, target_date)),
        "features": _run_inline(lambda: sync_mlb_features(session, target_date, None, True)),
        "market_family_discovery": _run_inline(lambda: run_market_family_discovery(session, target_date, force_refresh=False)),
        "market_family_mappings": _run_inline(lambda: sync_market_family_mappings(session, target_date)),
        "price_refresh": _run_inline(lambda: refresh_open_position_prices(session)),
        "feature_coverage": _run_inline(lambda: feature_coverage(session, target_date)),
    }


def _run_inline(fn: Callable[[], Any]) -> Any:
    return fn()


def _execute_job_steps(
    session: Session,
    run: JobRun,
    job_name: str,
    target_date: date | None,
    *,
    min_time_to_start_minutes: int | None = None,
    max_time_to_start_minutes: int | None = None,
    sweep_label: str | None = None,
    dry_run_candidates_only: bool = False,
) -> dict[str, object]:
    target = target_date or today_eastern()
    if job_name == "daily-setup":
        return {
            "schedule": _run_step(run, "sync_mlb_schedule", lambda: {"games": sync_schedule(session, target)}),
            "results_yesterday": _run_step(run, "sync_mlb_results_yesterday", lambda: sync_results(session, target - timedelta(days=1))),
            "results_today": _run_step(run, "sync_mlb_results_today", lambda: sync_results(session, target)),
            "features": _run_step(run, "sync_mlb_features", lambda: sync_mlb_features(session, target, None, True)),
            "market_family_discovery": _run_step(
                run,
                "market_family_discovery",
                lambda: run_market_family_discovery(session, target, force_refresh=False),
            ),
            "market_family_mappings": _run_step(
                run, "market_family_mappings", lambda: sync_market_family_mappings(session, target)
            ),
            "price_refresh": _run_step(run, "price_refresh", lambda: refresh_open_position_prices(session)),
            "balance_snapshot": _run_step(
                run, "balance_snapshot", lambda: {"snapshot_id": create_balance_snapshot(session, source="daily_setup").id}
            ),
            "feature_coverage": _run_step(run, "feature_coverage", lambda: feature_coverage(session, target)),
        }
    if job_name == "candidate-sweep":
        result = {
            "schedule": _run_step(run, "sync_mlb_schedule", lambda: {"games": sync_schedule(session, target)}),
            "features": _run_step(run, "sync_mlb_features", lambda: sync_mlb_features(session, target, None, True)),
            "market_family_mappings": _run_step(
                run, "market_family_mappings", lambda: sync_market_family_mappings(session, target)
            ),
        }
        candidate_result = _run_step(
            run,
            "paper_candidate_engine",
            lambda: generate_candidates(
                session,
                target,
                min_time_to_start_minutes=min_time_to_start_minutes,
                max_time_to_start_minutes=max_time_to_start_minutes,
                sweep_label=sweep_label,
                dry_run_candidates_only=dry_run_candidates_only,
            ),
        )
        result["candidate_engine"] = candidate_result
        result["candidate_sweep_window"] = _candidate_sweep_window_result(candidate_result)
        if dry_run_candidates_only:
            result["price_refresh"] = {"skipped": True, "reason": "dry_run_candidates_only"}
            result["balance_snapshot"] = {"skipped": True, "reason": "dry_run_candidates_only", "snapshot_id": None}
        else:
            result["price_refresh"] = _run_step(run, "price_refresh", lambda: refresh_open_position_prices(session))
            result["balance_snapshot"] = _run_step(
                run, "balance_snapshot", lambda: {"snapshot_id": create_balance_snapshot(session, source="candidate_sweep").id}
            )
        return result
    if job_name == "price-refresh":
        return {
            "price_refresh": _run_step(run, "price_refresh", lambda: refresh_open_position_prices(session)),
            "balance_snapshot": _run_step(
                run, "balance_snapshot", lambda: {"snapshot_id": create_balance_snapshot(session, source="price_refresh").id}
            ),
        }
    if job_name == "settlement":
        return {
            "results": _run_step(run, "sync_mlb_results", lambda: sync_results(session, target)),
            "settlement": _run_step(run, "paper_settlement_sync", lambda: settle_paper_trades(session, target)),
            "balance_snapshot": _run_step(
                run, "balance_snapshot", lambda: {"snapshot_id": create_balance_snapshot(session, source="settlement_job").id}
            ),
        }
    if job_name == "governance":
        return {
            "governance": _run_step(
                run,
                "model_governance",
                lambda: run_model_governance(session, paper_trading_epoch_id=run.paper_trading_epoch_id),
            )
        }
    if job_name == "full-paper-cycle":
        return {
            "daily_setup": _run_step(run, "daily_setup", lambda: _daily_setup_steps(session, target)),
            "candidate_sweep": _run_step(run, "candidate_sweep", lambda: generate_candidates(session, target)),
            "price_refresh": _run_step(run, "price_refresh", lambda: refresh_open_position_prices(session)),
            "settlement_yesterday": _run_step(
                run, "settlement_yesterday", lambda: settle_paper_trades(session, target - timedelta(days=1))
            ),
            "governance": _run_step(
                run,
                "model_governance",
                lambda: run_model_governance(session, paper_trading_epoch_id=run.paper_trading_epoch_id),
            ),
        }
    raise ValueError(f"Unknown job: {job_name}")


def run_job(
    session: Session,
    *,
    job_name: str,
    target_date: date | None = None,
    triggered_by: str = "manual",
    max_runtime_minutes: int = 60,
    min_time_to_start_minutes: int | None = None,
    max_time_to_start_minutes: int | None = None,
    sweep_label: str | None = None,
    dry_run_candidates_only: bool = False,
) -> dict[str, object]:
    _validate_candidate_sweep_options(min_time_to_start_minutes, max_time_to_start_minutes)
    run, acquired = acquire_job_lock(
        session,
        job_name=job_name,
        target_date=target_date,
        triggered_by=triggered_by,
        max_runtime_minutes=max_runtime_minutes,
    )
    if not acquired:
        session.commit()
        return {"job_run_id": run.id, "status": run.status, **(run.result or {})}

    run_id = run.id
    session.commit()
    try:
        if _candidate_sweep_options_enabled(
            min_time_to_start_minutes,
            max_time_to_start_minutes,
            sweep_label,
            dry_run_candidates_only,
        ):
            result = _execute_job_steps(
                session,
                run,
                job_name,
                run.target_date,
                min_time_to_start_minutes=min_time_to_start_minutes,
                max_time_to_start_minutes=max_time_to_start_minutes,
                sweep_label=sweep_label,
                dry_run_candidates_only=dry_run_candidates_only,
            )
        else:
            result = _execute_job_steps(session, run, job_name, run.target_date)
        run.status = "succeeded"
        run.result = result
        errors: list[object] = []
    except Exception as exc:
        failed_steps = list(run.__dict__.get("steps") or [])
        session.rollback()
        run = session.get(JobRun, run_id)
        if run is None:
            raise
        run.status = "failed"
        run.result = {"status": "failed"}
        if failed_steps:
            run.steps = failed_steps
        errors = [{"message": str(exc), "type": exc.__class__.__name__}]
        run.errors = [*list(run.errors or []), *errors]
    run.completed_at = utc_now()
    run.heartbeat_at = run.completed_at
    run.duration_seconds = _duration_seconds(run.started_at)
    session.add(run)
    session.commit()
    return {
        "job_run_id": run.id,
        "job_name": run.job_name,
        "target_date": run.target_date.isoformat() if run.target_date else None,
        "status": run.status,
        "duration_seconds": run.duration_seconds,
        "steps": run.steps or [],
        "result": run.result or {},
        "errors": errors if run.status == "failed" else [],
    }
