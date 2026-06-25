from __future__ import annotations

from app.config import get_settings
from app.database import get_session_factory
from app.services.market_sync import sync_kalshi_markets


def main() -> None:
    settings = get_settings()
    if not settings.market_discovery_enabled:
        print("Kalshi market discovery is disabled.")
        return

    session_factory = get_session_factory()
    with session_factory() as session:
        count = sync_kalshi_markets(session)

    print(f"Synced {count} Kalshi MLB candidate markets.")


if __name__ == "__main__":
    main()
