"""B2.5-04 tests: parent rollup on child completion.

`maybe_close_parent` (app.workers.rollup) is called from every terminal
status write (cancel_run, fake_runner, the real executor, the reaper) --
these tests exercise it directly plus its two real call sites (cancel_run,
reap_stale_runs) and the reaper's belt-and-suspenders reconciliation pass.
No B2.6 fan-out endpoint exists yet, so batches are seeded directly via SQL,
same as tests/test_run_call_aggregate.py.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.main import app
from app.workers.reaper import reap_stale_runs, reconcile_orphaned_parents
from app.workers.rollup import maybe_close_parent
from tests.conftest import _test_engine, auth_headers, requires_test_db

pytestmark = requires_test_db

_HEADERS = auth_headers()


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_HEADERS)


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = _test_engine()
    yield eng
    await eng.dispose()


async def _seed_agent(conn: AsyncConnection, *, max_concurrency: int = 5) -> uuid.UUID:
    agent_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO agents (id, name, transport, max_concurrency, created_by_user_id) "
            "VALUES (:id, 'Rollup Test Agent', 'web', :max_concurrency, 'user-1')"
        ),
        {"id": agent_id, "max_concurrency": max_concurrency},
    )
    return agent_id


async def _seed_parent(conn: AsyncConnection, agent_id: uuid.UUID) -> uuid.UUID:
    parent_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO runs (id, type, status, agent_id, created_by_user_id) "
            "VALUES (:id, 'suite', 'running', :agent_id, 'user-1')"
        ),
        {"id": parent_id, "agent_id": agent_id},
    )
    return parent_id


async def _seed_child(
    conn: AsyncConnection, agent_id: uuid.UUID, parent_id: uuid.UUID, *, status: str = "queued"
) -> uuid.UUID:
    child_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO runs (id, type, status, agent_id, parent_run_id, created_by_user_id) "
            "VALUES (:id, 'simulation', :status, :agent_id, :parent_id, 'user-1')"
        ),
        {"id": child_id, "status": status, "agent_id": agent_id, "parent_id": parent_id},
    )
    return child_id


async def _run_row(engine: AsyncEngine, run_id: uuid.UUID) -> dict[str, object]:
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT status, ended_at FROM runs WHERE id = :id"), {"id": run_id}
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def _event_types(engine: AsyncEngine, run_id: uuid.UUID) -> list[str]:
    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text("SELECT type, data FROM run_events WHERE run_id = :id ORDER BY seq"),
                    {"id": run_id},
                )
            )
            .mappings()
            .all()
        )
    return [row["type"] for row in rows]


async def _cleanup(engine: AsyncEngine, agent_id: uuid.UUID) -> None:
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "DELETE FROM run_events "
                "WHERE run_id IN (SELECT id FROM runs WHERE agent_id = :id)"
            ),
            {"id": agent_id},
        )
        await conn.execute(text("DELETE FROM runs WHERE agent_id = :id"), {"id": agent_id})
        await conn.execute(text("DELETE FROM agents WHERE id = :id"), {"id": agent_id})


async def test_maybe_close_parent_noop_for_run_without_a_parent(engine: AsyncEngine) -> None:
    async with engine.connect() as conn, conn.begin():
        agent_id = await _seed_agent(conn)
        run_id = await _seed_parent(conn, agent_id)  # parent_run_id IS NULL
    try:
        async with engine.connect() as conn, conn.begin():
            await maybe_close_parent(conn, run_id)
        row = await _run_row(engine, run_id)
        assert row["status"] == "running"  # untouched
        assert await _event_types(engine, run_id) == []
    finally:
        await _cleanup(engine, agent_id)


async def test_parent_stays_open_until_last_child_terminal(engine: AsyncEngine) -> None:
    async with engine.connect() as conn, conn.begin():
        agent_id = await _seed_agent(conn)
        parent_id = await _seed_parent(conn, agent_id)
        children = [await _seed_child(conn, agent_id, parent_id) for _ in range(3)]
    try:
        # Finish two of three children.
        for child_id in children[:2]:
            async with engine.connect() as conn, conn.begin():
                await conn.execute(
                    text("UPDATE runs SET status = 'completed', ended_at = now() WHERE id = :id"),
                    {"id": child_id},
                )
                await maybe_close_parent(conn, child_id)

        row = await _run_row(engine, parent_id)
        assert row["status"] == "running"
        assert row["ended_at"] is None
        parent_events = await _event_types(engine, parent_id)
        assert parent_events == ["progress", "progress"]

        # Finish the last one.
        async with engine.connect() as conn, conn.begin():
            await conn.execute(
                text("UPDATE runs SET status = 'failed', ended_at = now() WHERE id = :id"),
                {"id": children[2]},
            )
            await maybe_close_parent(conn, children[2])

        row = await _run_row(engine, parent_id)
        assert row["status"] == "completed"
        assert row["ended_at"] is not None
        assert await _event_types(engine, parent_id) == [
            "progress",
            "progress",
            "progress",
            "status",
        ]
    finally:
        await _cleanup(engine, agent_id)


async def test_maybe_close_parent_is_a_noop_once_parent_already_terminal(
    engine: AsyncEngine,
) -> None:
    """Extending a closed batch (e.g. a late reaper tick after
    reconcile_orphaned_parents already closed it) must not re-close it or
    emit a second `status` event."""
    async with engine.connect() as conn, conn.begin():
        agent_id = await _seed_agent(conn)
        parent_id = await _seed_parent(conn, agent_id)
        child_id = await _seed_child(conn, agent_id, parent_id, status="completed")
        await conn.execute(
            text("UPDATE runs SET status = 'completed', ended_at = now() WHERE id = :id"),
            {"id": parent_id},
        )
    try:
        async with engine.connect() as conn, conn.begin():
            await maybe_close_parent(conn, child_id)
        assert await _event_types(engine, parent_id) == []
    finally:
        await _cleanup(engine, agent_id)


async def test_cancel_run_closes_parent_when_it_was_the_last_live_child(
    engine: AsyncEngine,
) -> None:
    """B2.6-02: the parent closes 'cancelled', not 'completed', once any
    child ended up cancelled -- distinct from test_parent_stays_open_until_
    last_child_terminal's all-'completed'/'failed' case below."""
    async with engine.connect() as conn, conn.begin():
        agent_id = await _seed_agent(conn)
        parent_id = await _seed_parent(conn, agent_id)
        done_child = await _seed_child(conn, agent_id, parent_id, status="completed")
        live_child = await _seed_child(conn, agent_id, parent_id, status="queued")
    try:
        async with await _client() as client:
            response = await client.post(f"/v1/runs/{live_child}/cancel")
        assert response.status_code == 204

        row = await _run_row(engine, parent_id)
        assert row["status"] == "cancelled"
        assert row["ended_at"] is not None
        _ = done_child
    finally:
        await _cleanup(engine, agent_id)


async def test_reap_stale_child_closes_parent_when_it_was_the_last_live_child(
    engine: AsyncEngine,
) -> None:
    """Sets the child straight to 'claimed' + a stale heartbeat via raw SQL
    rather than going through claim_run() -- claim_run() picks the globally
    oldest queued row in the whole (shared) test DB, which this test has no
    business depending on; reap_stale_runs's own query is what needs to
    find this exact row, and that's scoped by status/heartbeat, not id."""
    async with engine.connect() as conn, conn.begin():
        agent_id = await _seed_agent(conn)
        parent_id = await _seed_parent(conn, agent_id)
        await _seed_child(conn, agent_id, parent_id, status="completed")
        stale_child = await _seed_child(conn, agent_id, parent_id, status="claimed")
        await conn.execute(
            text(
                "UPDATE runs SET heartbeat_at = now() - interval '120 seconds', "
                "claimed_by = 'test-worker-rollup' WHERE id = :id"
            ),
            {"id": stale_child},
        )
    try:
        reaped = await reap_stale_runs(engine, threshold_seconds=60)
        assert stale_child in reaped

        row = await _run_row(engine, parent_id)
        assert row["status"] == "completed"
    finally:
        await _cleanup(engine, agent_id)


async def test_reconcile_orphaned_parents_closes_a_stuck_parent(engine: AsyncEngine) -> None:
    """Simulates the crash case: all children terminal, but nothing ever
    called maybe_close_parent (e.g. the process died between the child's
    status write and its rollup call, in two separate transactions)."""
    async with engine.connect() as conn, conn.begin():
        agent_id = await _seed_agent(conn)
        parent_id = await _seed_parent(conn, agent_id)
        await _seed_child(conn, agent_id, parent_id, status="completed")
        await _seed_child(conn, agent_id, parent_id, status="failed")
        # Deliberately no maybe_close_parent call -- parent_id is still 'running'.
    try:
        row = await _run_row(engine, parent_id)
        assert row["status"] == "running"

        reconciled = await reconcile_orphaned_parents(engine)
        assert parent_id in reconciled

        row = await _run_row(engine, parent_id)
        assert row["status"] == "completed"
        assert row["ended_at"] is not None
        assert await _event_types(engine, parent_id) == ["status"]
    finally:
        await _cleanup(engine, agent_id)


async def test_reconcile_orphaned_parents_ignores_batches_with_a_live_child(
    engine: AsyncEngine,
) -> None:
    async with engine.connect() as conn, conn.begin():
        agent_id = await _seed_agent(conn)
        parent_id = await _seed_parent(conn, agent_id)
        await _seed_child(conn, agent_id, parent_id, status="completed")
        await _seed_child(conn, agent_id, parent_id, status="running")
    try:
        reconciled = await reconcile_orphaned_parents(engine)
        assert parent_id not in reconciled
        row = await _run_row(engine, parent_id)
        assert row["status"] == "running"
    finally:
        await _cleanup(engine, agent_id)


async def test_concurrent_siblings_finishing_together_close_parent_exactly_once(
    engine: AsyncEngine,
) -> None:
    """B2.6 runs children at up to max_concurrency in parallel -- two
    siblings can reach maybe_close_parent at almost the same moment. The
    parent-row lock (app.workers.rollup) must serialize them so the parent
    closes exactly once, not zero or two times."""
    async with engine.connect() as conn, conn.begin():
        agent_id = await _seed_agent(conn)
        parent_id = await _seed_parent(conn, agent_id)
        children = [await _seed_child(conn, agent_id, parent_id) for _ in range(2)]

    async def _finish_and_close(child_id: uuid.UUID) -> None:
        async with engine.connect() as conn, conn.begin():
            await conn.execute(
                text("UPDATE runs SET status = 'completed', ended_at = now() WHERE id = :id"),
                {"id": child_id},
            )
            await maybe_close_parent(conn, child_id)

    try:
        await asyncio.gather(*(_finish_and_close(c) for c in children))

        row = await _run_row(engine, parent_id)
        assert row["status"] == "completed"
        status_events = [t for t in await _event_types(engine, parent_id) if t == "status"]
        assert len(status_events) == 1
    finally:
        await _cleanup(engine, agent_id)
