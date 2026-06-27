from __future__ import annotations

import os
import sys
from datetime import date

from app.config import get_settings
from app.database import get_session_factory
from app.services.candidates import generate_candidates
from app.services.market_sync import sync_kalshi_markets
from app.services.mlb import sync_schedule


def _target_date() -> date | None:
    value = sys.argv[1] if len(sys.argv) > 1 else os.getenv("TARGET_DATE")
    return date.fromisoformat(value) if value else None


def main() -> None:
    settings = get_settings()
    target_date = _target_date()
    session_factory = get_session_factory()
    with session_factory() as session:
        sync_schedule(session, target_date)
        if settings.market_discovery_enabled:
            try:
                sync_kalshi_markets(session)
            except Exception as exc:
                print(f"Kalshi market sync skipped after error: {exc}")
        result = generate_candidates(session, target_date)

    print(f"Candidate engine result: {result}")


if __name__ == "__main__":
    main()
