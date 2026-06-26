from __future__ import annotations

from datetime import date
import os

from app.database import get_session_factory
from app.services.features import sync_mlb_features


def _target_date() -> date | None:
    value = os.getenv("TARGET_DATE")
    return date.fromisoformat(value) if value else None


def main() -> None:
    session_factory = get_session_factory()
    with session_factory() as session:
        result = sync_mlb_features(session, _target_date())

    print(f"Model feature snapshot backfill result: {result}")


if __name__ == "__main__":
    main()
