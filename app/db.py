from functools import lru_cache

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings


def database_url() -> str | None:
    """The app's own database (ADR-0016), or None when it isn't configured.

    DATABASE_URL overrides everything (tests use it to point Alembic at a
    throwaway SQLite file). Otherwise the platform convention: user and
    database are both APP_NAME, the password arrives as POSTGRES_PASSWORD
    from the deploy-time .env, the host is `postgres` on home-platform.
    """
    if settings.database_url:
        return settings.database_url
    # No password configured means this app either hasn't been onboarded
    # to Postgres yet or doesn't use it -- both are valid states, not an
    # error, so callers get a clean "not connected" instead of a crash.
    if not settings.postgres_password:
        return None
    return (
        f"postgresql+psycopg2://{settings.app_name}:{settings.postgres_password}"
        f"@{settings.postgres_host}:5432/{settings.app_name}"
    )


@lru_cache(maxsize=1)
def _engine():
    url = database_url()
    return create_engine(url, pool_pre_ping=True) if url else None


class DatabaseNotConfigured(RuntimeError):
    pass


def session() -> Session:
    """A new ORM session on the app's database. Use as `with session() as s:`."""
    engine = _engine()
    if engine is None:
        raise DatabaseNotConfigured("no database configured (POSTGRES_PASSWORD unset)")
    return sessionmaker(engine, expire_on_commit=False)()


def check_connection() -> bool:
    engine = _engine()
    if engine is None:
        return False
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except SQLAlchemyError:
        return False
