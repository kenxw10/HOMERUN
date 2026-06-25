from __future__ import annotations

import argparse
from datetime import date

from app.database import get_session_factory
from app.services.mlb import sync_schedule


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync MLB schedule into mlb_games.")
    parser.add_argument("date", nargs="?", help="Optional schedule date in YYYY-MM-DD format.")
    args = parser.parse_args()
    target_date = date.fromisoformat(args.date) if args.date else None

    session_factory = get_session_factory()
    with session_factory() as session:
        count = sync_schedule(session, target_date)

    print(f"Synced {count} MLB games.")


if __name__ == "__main__":
    main()
