from __future__ import annotations

from app.database import get_session_factory
from app.services.modeling import repair_training_eligibility


def main() -> None:
    session_factory = get_session_factory()
    with session_factory() as session:
        result = repair_training_eligibility(session)

    print(f"Training eligibility repair result: {result}")


if __name__ == "__main__":
    main()
