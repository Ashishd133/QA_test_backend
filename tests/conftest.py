from collections.abc import Generator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import get_settings
from app.db import build_engine, get_engine
from app.main import app

requires_test_db = pytest.mark.skipif(
    not get_settings().test_database_url, reason="TEST_DATABASE_URL not configured"
)


def _test_engine() -> AsyncEngine:
    """The shared production DB has a live worker continuously polling for
    queued runs (spine §5) -- it will claim and execute a test-inserted
    'queued' row out from under the test before the test's own assertions
    run (discovered during B1-01). Every DB-touching test must build its
    engine from this isolated Neon branch instead of app.db.get_engine()'s
    default, production database_url.

    null_pool=True: asyncpg connections bind to the event loop that created
    them, and pytest-asyncio hands each test function its own loop.
    """
    return build_engine(database_url=get_settings().test_database_url, null_pool=True)


@pytest.fixture(autouse=True)
def _override_engine_for_tests() -> Generator[None]:
    """Same event-loop-per-test issue as _test_engine()'s docstring, for
    routes exercised via a real HTTP request through `app` (Depends(get_engine)
    otherwise resolves to the cached, pooled, production-pointed engine)."""
    app.dependency_overrides[get_engine] = _test_engine
    yield
    app.dependency_overrides.pop(get_engine, None)
