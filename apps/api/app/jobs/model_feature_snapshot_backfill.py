from __future__ import annotations

import argparse
from datetime import date
import os

from app.database import get_session_factory
from app.services.features import sync_mlb_features


def _target_date(value: str | None = None) -> date | None:
    value = value or os.getenv("TARGET_DATE")
    return date.fromisoformat(value) if value else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill mature model feature snapshots.")
    parser.add_argument("date", nargs="?", help="Optional feature date in YYYY-MM-DD format.")
    args = parser.parse_args()

    session_factory = get_session_factory()
    with session_factory() as session:
        result = sync_mlb_features(session, _target_date(args.date))

    print(f"Model feature snapshot backfill result: {result}")


if __name__ == "__main__":
    main()
