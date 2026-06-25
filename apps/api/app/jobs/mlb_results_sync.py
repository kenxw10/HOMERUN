from __future__ import annotations

import argparse
from datetime import date

from app.database import get_session_factory
from app.services.mlb import sync_results


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync MLB results into mlb_games.")
    parser.add_argument("date", nargs="?", help="Optional results date in YYYY-MM-DD format.")
    args = parser.parse_args()
    target_date = date.fromisoformat(args.date) if args.date else None

    session_factory = get_session_factory()
    with session_factory() as session:
        result = sync_results(session, target_date)

    print(f"MLB results sync result: {result}")


if __name__ == "__main__":
    main()
