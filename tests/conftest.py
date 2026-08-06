from collections.abc import AsyncGenerator

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

    Callers that build one directly (rather than through the
    `_override_engine_for_tests` fixture) are responsible for disposing it
    before the test ends. Note the full-suite hang documented in memory
    (test-suite-resource-exhaustion / B1-08) turned out NOT to be an
    undisposed-engine/cross-loop-GC issue (measured directly: forcing
    gc.collect() after every test didn't change the hang pattern) -- it's
    connection *establishment* to this (distant, us-east-1) Neon branch
    occasionally stalling well past asyncpg's own connect timeout (a live
    capture during a stall showed an ESTABLISHED TCP socket stuck mid
    handshake), multiplied by null_pool opening a fresh connection per
    checkout. See pyproject.toml's pytest `timeout` setting, which bounds
    the resulting occasional slow test instead of letting it hang forever.
    """
    return build_engine(database_url=get_settings().test_database_url, null_pool=True)


@pytest.fixture(autouse=True)
async def _override_engine_for_tests() -> AsyncGenerator[None]:
    """Same event-loop-per-test issue as _test_engine()'s docstring, for
    routes exercised via a real HTTP request through `app` (Depends(get_engine)
    otherwise resolves to the cached, pooled, production-pointed engine).

    One engine per test, reused across every `Depends(get_engine)`
    resolution and disposed here before the test's event loop is torn down
    -- previously this built a fresh, never-disposed engine on every single
    HTTP request across all 94 tests, the single largest source of the
    undisposed-engine leak (see _test_engine's docstring).
    """
    engine = _test_engine()
    app.dependency_overrides[get_engine] = lambda: engine
    yield
    app.dependency_overrides.pop(get_engine, None)
    await engine.dispose()
