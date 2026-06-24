from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def get_engine():
    settings = get_settings()
    if not settings.sqlalchemy_database_url:
        raise RuntimeError("DATABASE_URL is not configured.")
    return create_engine(settings.sqlalchemy_database_url, pool_pre_ping=True)


def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, autocommit=False)


def get_db_session() -> Generator[Session, None, None]:
    session_factory = get_session_factory()
    db = session_factory()
    try:
        yield db
    finally:
        db.close()


def database_status() -> dict[str, object]:
    settings = get_settings()
    configured = bool(settings.sqlalchemy_database_url)
    dialect = None

    if settings.sqlalchemy_database_url:
        dialect = settings.sqlalchemy_database_url.split(":", 1)[0]

    if not configured:
        return {
            "ready": False,
            "configured": False,
            "dialect": None,
            "message": "DATABASE_URL is not set.",
        }

    engine = None
    try:
        engine = create_engine(
            settings.sqlalchemy_database_url,
            pool_pre_ping=True,
            connect_args={"connect_timeout": 2},
        )
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return {
            "ready": False,
            "configured": True,
            "dialect": dialect,
            "message": "Database connection failed; check DATABASE_URL and network access.",
        }
    finally:
        if engine is not None:
            engine.dispose()

    return {
        "ready": True,
        "configured": True,
        "dialect": dialect,
        "message": "Database connection verified.",
    }
