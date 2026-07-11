from __future__ import annotations

import argparse
from datetime import UTC, datetime, time, timedelta
import json
import sys
from zoneinfo import ZoneInfo

from app.database import get_session_factory
from app.services.job_runs import JOB_NAMES, resolve_job_target_date, run_job

ET_ZONE = ZoneInfo("America/New_York")


def _json_safe(value):
    if isinstance(value, (datetime,)):
        return value.isoformat()
    return str(value)


def _log_event(event: str, **payload: object) -> None:
    print(json.dumps({"event": event, **payload}, default=_json_safe, sort_keys=True), flush=True)


def _target_window_payload(target_date) -> dict[str, object]:
    if target_date is None:
        return {
            "target_date": None,
            "target_window_start_et": None,
            "target_window_end_et": None,
            "target_window_start_utc": None,
            "target_window_end_utc": None,
        }
    start_et = datetime.combine(target_date, time.min, tzinfo=ET_ZONE)
    end_et = start_et + timedelta(days=1)
    return {
        "target_date": target_date.isoformat(),
        "target_window_start_et": start_et.isoformat(),
        "target_window_end_et": end_et.isoformat(),
        "target_window_start_utc": start_et.astimezone(UTC).isoformat(),
        "target_window_end_utc": end_et.astimezone(UTC).isoformat(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one cron-safe HOMERUN paper operations job.")
    parser.add_argument("--job", required=True, choices=sorted(JOB_NAMES))
    parser.add_argument("--target-date", default=None, help="YYYY-MM-DD, today_et, or yesterday_et.")
    parser.add_argument("--triggered-by", default="cron", choices=["manual", "cron", "api"])
    parser.add_argument("--max-runtime-minutes", type=int, default=60)
    parser.add_argument("--min-time-to-start-minutes", type=int, default=None)
    parser.add_argument("--max-time-to-start-minutes", type=int, default=None)
    parser.add_argument("--sweep-label", default=None)
    parser.add_argument(
        "--dry-run-candidates-only",
        default="false",
        choices=["true", "false"],
        help="Score and save labeled candidate diagnostics without opening paper trades.",
    )
    args = parser.parse_args()

    _log_event(
        "cron_startup",
        job=args.job,
        target_date_arg=args.target_date,
        triggered_by=args.triggered_by,
        max_runtime_minutes=args.max_runtime_minutes,
        dry_run_candidates_only=args.dry_run_candidates_only == "true",
    )
    try:
        target_date = resolve_job_target_date(args.target_date)
        _log_event("target_date_resolved", job=args.job, **_target_window_payload(target_date))
        session_factory = get_session_factory()
        with session_factory() as session:
            result = run_job(
                session,
                job_name=args.job,
                target_date=target_date,
                triggered_by=args.triggered_by,
                max_runtime_minutes=args.max_runtime_minutes,
                min_time_to_start_minutes=args.min_time_to_start_minutes,
                max_time_to_start_minutes=args.max_time_to_start_minutes,
                sweep_label=args.sweep_label,
                dry_run_candidates_only=args.dry_run_candidates_only == "true",
            )
    except Exception as exc:
        _log_event("caught_exception_failure", job=args.job, error_type=exc.__class__.__name__, error=str(exc))
        raise

    warnings = result.get("warnings") if isinstance(result.get("warnings"), list) else []
    _log_event(
        "lock_status",
        job=args.job,
        job_run_id=result.get("job_run_id"),
        status=result.get("status"),
        skipped_reason=result.get("skipped_reason"),
        existing_run_id=result.get("existing_run_id"),
    )
    _log_event(
        "stale_run_recovery_decision",
        job=args.job,
        recovered_stale_runs=len(warnings),
        recovered=bool(warnings),
        warnings=warnings,
    )
    if args.job == "settlement":
        run_result = result.get("result") if isinstance(result.get("result"), dict) else {}
        settlement_result = run_result.get("settlement") if isinstance(run_result.get("settlement"), dict) else {}
        balance_snapshot = run_result.get("balance_snapshot") if isinstance(run_result.get("balance_snapshot"), dict) else {}
        _log_event(
            "settlement_query_scope",
            job=args.job,
            bounded_target_date=settlement_result.get("bounded_target_date"),
            active_epoch_id=settlement_result.get("bounded_active_epoch_id"),
            **_target_window_payload(target_date),
        )
        _log_event(
            "settlement_batch_status",
            job=args.job,
            candidate_label_batch_limit=settlement_result.get("settlement_candidate_label_batch_limit"),
            audit_backfill_batch_limit=settlement_result.get("settlement_audit_backfill_batch_limit"),
            open_trade_batch_limit=settlement_result.get("settlement_open_trade_batch_limit"),
            candidate_labels_checked=settlement_result.get("candidate_labels_checked"),
            audit_backfill_candidates_checked=settlement_result.get("audit_backfill_candidates_checked"),
            checked=settlement_result.get("checked"),
            candidate_labels_limited_by_batch_cap=settlement_result.get("candidate_labels_limited_by_batch_cap"),
            audit_backfill_limited_by_batch_cap=settlement_result.get("audit_backfill_limited_by_batch_cap"),
            open_trade_settlement_limited_by_batch_cap=settlement_result.get(
                "open_trade_settlement_limited_by_batch_cap"
            ),
            warnings=settlement_result.get("warnings"),
        )
        _log_event(
            "settlement_counts",
            job=args.job,
            checked=settlement_result.get("checked"),
            settled=settlement_result.get("settled"),
            already_settled=settlement_result.get("already_settled"),
            already_settled_audit_backfilled=settlement_result.get("already_settled_audit_backfilled"),
            skipped_not_final=settlement_result.get("skipped_not_final"),
            skipped_unsupported=settlement_result.get("skipped_unsupported"),
            skipped_parse_uncertain=settlement_result.get("skipped_parse_uncertain"),
            skipped_invalid_selection=settlement_result.get("skipped_invalid_selection"),
            skip_reasons=settlement_result.get("skip_reasons"),
        )
        _log_event(
            "balance_snapshot_action",
            job=args.job,
            snapshot_id=balance_snapshot.get("snapshot_id"),
        )
    if args.job == "spread-audit":
        run_result = result.get("result") if isinstance(result.get("result"), dict) else {}
        spread_result = run_result.get("spread_audit") if isinstance(run_result.get("spread_audit"), dict) else {}
        _log_event(
            "spread_audit_window",
            job=args.job,
            min_time_to_start_minutes=args.min_time_to_start_minutes,
            max_time_to_start_minutes=args.max_time_to_start_minutes,
            audit_target_date=spread_result.get("target_date"),
            **_target_window_payload(target_date),
        )
        _log_event(
            "spread_audit_coverage",
            job=args.job,
            target_date_mapping_count=spread_result.get("target_date_mapping_count"),
            target_date_distinct_market_count=spread_result.get("target_date_distinct_market_count"),
            target_date_distinct_game_count=spread_result.get("target_date_distinct_game_count"),
            in_window_mapping_count=spread_result.get("in_window_mapping_count"),
            checked=spread_result.get("checked"),
            coverage_ratio=spread_result.get("coverage_ratio"),
            coverage_status=spread_result.get("coverage_status"),
            zero_checked_reason=spread_result.get("zero_checked_reason"),
            skipped_before_min_window_count=spread_result.get("skipped_before_min_window_count"),
            skipped_after_max_window_count=spread_result.get("skipped_after_max_window_count"),
        )
        _log_event(
            "spread_audit_result_counts",
            job=args.job,
            verified=spread_result.get("verified"),
            trusted_audit_only_count=spread_result.get("trusted_audit_only_count"),
            needs_review_count=spread_result.get("needs_review_count"),
            unsafe_count=spread_result.get("unsafe_count"),
            parse_error_count=spread_result.get("parse_error_count"),
            paper_trades_created=spread_result.get("paper_trades_created"),
            mapping_mutations=spread_result.get("mapping_mutations"),
            settlement_rows_created=spread_result.get("settlement_rows_created"),
            audit_only=spread_result.get("audit_only"),
            read_only=spread_result.get("read_only"),
        )
        _log_event(
            "spread_audit_warning",
            job=args.job,
            coverage_warning=spread_result.get("coverage_status")
            in {"no_mappings_in_window", "partial_coverage", "zero_checked_with_eligible_mappings", "unknown"},
            zero_checked_reason=spread_result.get("zero_checked_reason"),
        )

    if result.get("status") == "failed":
        _log_event("job_failed", job=args.job, job_run_id=result.get("job_run_id"), errors=result.get("errors"))
    _log_event(
        "clean_completion",
        job=args.job,
        job_run_id=result.get("job_run_id"),
        status=result.get("status"),
        duration_seconds=result.get("duration_seconds"),
    )
    if result.get("status") in {"failed", "failed_stale"}:
        sys.exit(1)


if __name__ == "__main__":
    main()
