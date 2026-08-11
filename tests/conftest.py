import asyncio.constants
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import get_settings
from app.db import build_engine, get_engine
from app.main import app
from app.models.projects import DEFAULT_PROJECT_ID

# Diagnosed 2026-08 (see test-suite-resource-exhaustion memory): an
# occasional stalled `getaddrinfo()` call -- a blocking OS call, run in the
# event loop's default executor thread pool, that cannot be cancelled once
# started -- leaves a zombie thread behind when asyncpg's own connect
# `timeout` gives up waiting on it. pytest-asyncio hands every test its own
# `asyncio.Runner`; `Runner.close()` then calls
# `shutdown_default_executor(constants.THREAD_JOIN_TIMEOUT)`, which by
# default waits up to 300s for that zombie thread to be joined before
# abandoning it -- five minutes, comfortably past pyproject.toml's 120s
# pytest-timeout bound, so the whole `pytest` process gets hard-killed
# instead of just that one test failing. Confirmed directly: patching this
# constant down turns a 300s block into ~2s (thread abandoned, one
# RuntimeWarning, teardown proceeds) -- see the same test verified with
# `asyncio.Runner` standalone. This does NOT prevent the underlying
# stall (that's real, external network flakiness to a distant Neon branch,
# not fixable here) -- it only stops one stalled connection from taking the
# entire suite down with it; the one test that hit the stall still fails.
# mypy's stub declares this Final -- true for library code, but this is the
# one legitimate reason to override it: a test-process-only tuning knob for
# stdlib teardown behavior, not a redefinition of the constant's meaning.
asyncio.constants.THREAD_JOIN_TIMEOUT = 5  # type: ignore[misc]

requires_test_db = pytest.mark.skipif(
    not get_settings().test_database_url, reason="TEST_DATABASE_URL not configured"
)


def auth_headers(
    *, user_id: str | None = "user-1", project_id: object = DEFAULT_PROJECT_ID
) -> dict[str, str]:
    """Shared header builder for project-scoped routes (B2.5-01). Every
    test module that exercises agents/suites/runs through a real HTTP
    request needs X-Project-Id now that those tables are hard-scoped --
    centralized here so B2.6's next scoped surface doesn't mean editing
    ten modules again. `user_id=None` omits X-User-Id, for the negative
    "missing header" tests."""
    headers = {"Authorization": f"Bearer {get_settings().python_service_token}"}
    if user_id is not None:
        headers["X-User-Id"] = user_id
    if project_id is not None:
        headers["X-Project-Id"] = str(project_id)
    return headers


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
    gc.collect() after every test didn't change the hang pattern). Two
    distinct network-flakiness signatures to this (distant, us-east-1) Neon
    branch have been observed, both multiplied by null_pool opening a fresh
    connection per checkout:
      1. Connection establishment stalling past asyncpg's own connect
         timeout (a live capture during a stall showed an ESTABLISHED TCP
         socket stuck mid handshake, ~18s against a configured 10s) -- see
         pyproject.toml's pytest `timeout`, which bounds the resulting
         occasional slow test.
      2. A stalled `getaddrinfo()` call (surfaces as `socket.gaierror` or a
         plain hang) -- see this module's `THREAD_JOIN_TIMEOUT` patch above
         for why that one could take down the whole pytest process, not
         just one test, before that patch existed.
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
