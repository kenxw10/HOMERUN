from __future__ import annotations

from app.database import get_session_factory
from app.services.position_refresh import refresh_open_position_prices


def main() -> None:
    session_factory = get_session_factory()
    with session_factory() as session:
        result = refresh_open_position_prices(session)

    print(f"Open position price refresh result: {result}")


if __name__ == "__main__":
    main()
