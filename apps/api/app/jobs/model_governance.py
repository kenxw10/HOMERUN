from __future__ import annotations

from app.database import get_session_factory
from app.services.modeling import run_model_governance


def main() -> None:
    session_factory = get_session_factory()
    with session_factory() as session:
        result = run_model_governance(session)

    print(f"Model governance result: {result}")


if __name__ == "__main__":
    main()
