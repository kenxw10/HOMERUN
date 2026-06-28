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
        )

    print(result)
    if result.get("status") in {"failed", "failed_stale"}:
        sys.exit(1)


if __name__ == "__main__":
    main()
