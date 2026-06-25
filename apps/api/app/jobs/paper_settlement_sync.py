from __future__ import annotations

import argparse
from datetime import date

from app.database import get_session_factory
from app.services.settlement import settle_paper_trades


def main() -> None:
    parser = argparse.ArgumentParser(description="Settle supported full-game winner paper trades.")
    parser.add_argument("date", nargs="?", help="Optional game date in YYYY-MM-DD format.")
    args = parser.parse_args()
    target_date = date.fromisoformat(args.date) if args.date else None

    session_factory = get_session_factory()
    with session_factory() as session:
        result = settle_paper_trades(session, target_date)

    print(f"Paper settlement sync result: {result}")


if __name__ == "__main__":
    main()
