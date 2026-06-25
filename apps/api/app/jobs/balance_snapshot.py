from __future__ import annotations

from app.database import get_session_factory
from app.services.portfolio import create_balance_snapshot


def main() -> None:
    session_factory = get_session_factory()
    with session_factory() as session:
        snapshot = create_balance_snapshot(session, source="balance_snapshot_job")
        snapshot_id = snapshot.id
        cash_balance = snapshot.cash_balance
        portfolio_value = snapshot.portfolio_value
        session.commit()

    print(
        "Balance snapshot created: "
        f"id={snapshot_id} cash={cash_balance} portfolio={portfolio_value}"
    )


if __name__ == "__main__":
    main()
