from __future__ import annotations

import argparse
import sys

from app.database import get_session_factory
from app.services.job_runs import JOB_NAMES, resolve_job_target_date, run_job


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

    target_date = resolve_job_target_date(args.target_date)
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

    print(result)
    if result.get("status") in {"failed", "failed_stale"}:
        sys.exit(1)


if __name__ == "__main__":
    main()
