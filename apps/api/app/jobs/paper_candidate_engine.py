from __future__ import annotations

from app.config import get_settings
from app.database import get_session_factory
from app.services.candidates import generate_candidates
from app.services.market_sync import sync_kalshi_markets
from app.services.mlb import sync_schedule


def main() -> None:
    settings = get_settings()
    session_factory = get_session_factory()
    with session_factory() as session:
        sync_schedule(session)
        if settings.market_discovery_enabled:
            try:
                sync_kalshi_markets(session)
            except Exception as exc:
                print(f"Kalshi market sync skipped after error: {exc}")
        result = generate_candidates(session)

    print(f"Candidate engine result: {result}")


if __name__ == "__main__":
    main()
