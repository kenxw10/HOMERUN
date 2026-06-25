from __future__ import annotations

import sys
from datetime import date

from app.database import get_session_factory
from app.services.market_family_discovery import run_market_family_discovery


def _target_date_from_args() -> date | None:
    if len(sys.argv) < 2:
        return None
    return date.fromisoformat(sys.argv[1])


def main() -> None:
    session_factory = get_session_factory()
    with session_factory() as session:
        result = run_market_family_discovery(session, _target_date_from_args())

    print(f"Market family discovery result: {result}")


if __name__ == "__main__":
    main()
