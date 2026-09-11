"""Database engines.

Two kinds of databases are involved:

* the **app database** — owned by this service (users, roles, permissions,
  workflow runs). SQLite by default, any SQLAlchemy async URL works;
* **data sources** — external SQL databases declared in `SQL_DATABASES`
  that the SQL specialist is allowed to query on the user's behalf.
"""

from datetime import UTC, datetime
from functools import cache
from pathlib import Path

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import StaticPool

from app.config import SqlDatabase, settings


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    """Naive UTC timestamp — stored consistently across SQLite / MySQL / Postgres."""
    return datetime.now(UTC).replace(tzinfo=None)


def _create_engine(url: str) -> AsyncEngine:
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        return create_async_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=10)

    if parsed.database and parsed.database != ":memory:":
        Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
        return create_async_engine(url)
    # A single shared connection keeps an in-memory database alive (used by tests).
    return create_async_engine(url, poolclass=StaticPool, connect_args={"check_same_thread": False})


@cache
def app_engine() -> AsyncEngine:
    return _create_engine(settings.app_database_url)


@cache
def _app_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(app_engine(), expire_on_commit=False)


def app_session() -> AsyncSession:
    return _app_sessionmaker()()


async def init_app_db() -> None:
    """Create app tables if they do not exist yet."""
    # Import models so that they are registered on Base.metadata.
    from app.permissions import models as _permissions  # noqa: F401
    from app.workflows import store as _workflows  # noqa: F401

    async with app_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# --- External data sources --------------------------------------------------


def list_databases() -> dict[str, SqlDatabase]:
    return settings.sql_databases


def get_database(name: str) -> SqlDatabase:
    try:
        return settings.sql_databases[name]
    except KeyError:
        known = ", ".join(settings.sql_databases) or "none configured"
        raise ValueError(f"Unknown database '{name}'. Available: {known}") from None


@cache
def data_engine(name: str) -> AsyncEngine:
    return _create_engine(get_database(name).url)
