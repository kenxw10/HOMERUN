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
        result = sync_kalshi_markets(session)

    print(
        "Kalshi targeted sync complete: "
        f"games={result['games_considered']} "
        f"markets={result['markets_upserted']} "
        f"mappings={result['mappings_created_or_updated']} "
        f"errors={len(result['errors'])}"
    )


if __name__ == "__main__":
    main()
