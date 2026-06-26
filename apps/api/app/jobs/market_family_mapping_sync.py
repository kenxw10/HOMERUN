from __future__ import annotations

import sys
from datetime import date

from app.database import get_session_factory
from app.services.market_family_mapping import sync_market_family_mappings


def _target_date_from_args() -> date | None:
    if len(sys.argv) < 2:
        return None
    return date.fromisoformat(sys.argv[1])


def main() -> int:
    SessionLocal = get_session_factory()
    with SessionLocal() as session:
        result = sync_market_family_mappings(session, _target_date_from_args())
    print(f"Market family mapping sync result: {result}")
    return 0 if result.get("status") != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
