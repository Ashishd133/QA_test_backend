from functools import lru_cache
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.config import get_settings


def to_asyncpg_url(database_url: str) -> tuple[str, dict[str, object]]:
    """Neon issues a libpq-style URL (`sslmode=require&channel_binding=require`).

    asyncpg's DSN parser doesn't understand those query params (they're libpq/
    psycopg concepts), so strip them from the URL and require TLS the asyncpg
    way instead, via connect_args.

    Also: use Neon's *direct* endpoint here, not the `-pooler` one — asyncpg's
    prepared-statement caching breaks under PgBouncer transaction pooling
    (the pooler is for serverless fan-out; this backend holds long-lived
    worker connections anyway, per spine §1).
    """
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL is not set (empty string) — check that the "
            "environment actually has it, not just a local .env file. "
            "SQLAlchemy's own error for this case ('Could not parse SQLAlchemy "
            "URL from given URL string') doesn't say which variable is missing."
        )
    parts = urlsplit(database_url)
    scheme = parts.scheme.replace("postgresql", "postgresql+asyncpg", 1)
    clean_url = urlunsplit((scheme, parts.netloc, parts.path, "", ""))
    # Without these, a connection that silently stalls mid-round-trip (a real,
    # repeatedly observed failure mode against Neon) hangs the awaiting
    # coroutine forever -- no exception, no timeout, nothing to catch or
    # retry. `timeout` bounds connection establishment; `command_timeout`
    # becomes asyncpg's per-statement default for every query issued on that
    # connection, turning an indefinite hang into a fast, catchable error.
    return clean_url, {"ssl": "require", "timeout": 10, "command_timeout": 30}


def build_engine(database_url: str | None = None, *, null_pool: bool = False) -> AsyncEngine:
    """null_pool=True gives a fresh connection per checkout — required for tests,
    since asyncpg connections are bound to the event loop that created them and
    pytest-asyncio hands each test a new loop; a pooled/cached engine reused
    across tests raises 'Future attached to a different loop'."""
    url = database_url if database_url is not None else get_settings().database_url
    clean_url, connect_args = to_asyncpg_url(url)
    kwargs: dict[str, object] = {"connect_args": connect_args}
    if null_pool:
        kwargs["poolclass"] = NullPool
    else:
        kwargs["pool_pre_ping"] = True
    return create_async_engine(clean_url, **kwargs)


@lru_cache
def get_engine() -> AsyncEngine:
    return build_engine()


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)
