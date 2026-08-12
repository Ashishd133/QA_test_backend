"""B2.6-02 tests: POST /v1/runs/{parentId}/cancel cascade.

Same endpoint as B1-05's single-run cancel (there's no separate route --
`/v1/runs/{run_id}/cancel` and `/v1/runs/{parentId}/cancel` are the same
path shape), now branching on whether `run_id` has children. Individual
child cancellation (the pre-B2.6 path, and the "parent closes 'cancelled'
not 'completed'" rule that applies there too) is covered in
tests/test_parent_rollup.py; this file is the cascade branch only: bulk
cancelling every live child in one transaction and closing the parent
immediately via app.workers.rollup.close_parent_now.
"""

import uuid
from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.main import app
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
            "VALUES (:id, 'Batch Cancel Agent', 'web', :max_concurrency, 'user-1')"
        ),
        {"id": agent_id, "max_concurrency": max_concurrency},
    )
    return agent_id


async def _seed_parent(
    conn: AsyncConnection, agent_id: uuid.UUID, *, status: str = "queued"
) -> uuid.UUID:
    parent_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO runs (id, type, status, agent_id, created_by_user_id) "
            "VALUES (:id, 'suite', :status, :agent_id, 'user-1')"
        ),
        {"id": parent_id, "status": status, "agent_id": agent_id},
    )
    return parent_id


async def _seed_child(
    conn: AsyncConnection, agent_id: uuid.UUID, parent_id: uuid.UUID, *, status: str
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


_STATUSES_SQL = text("SELECT id, status FROM runs WHERE id IN :ids").bindparams(
    bindparam("ids", expanding=True)
)


async def _statuses(engine: AsyncEngine, run_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    async with engine.connect() as conn:
        rows = (await conn.execute(_STATUSES_SQL, {"ids": run_ids})).mappings().all()
    return {row["id"]: row["status"] for row in rows}


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


async def test_batch_cancel_cascades_to_queued_and_running_children(
    engine: AsyncEngine,
) -> None:
    async with engine.connect() as conn, conn.begin():
        agent_id = await _seed_agent(conn)
        parent_id = await _seed_parent(conn, agent_id)
        queued = await _seed_child(conn, agent_id, parent_id, status="queued")
        running = await _seed_child(conn, agent_id, parent_id, status="running")
        claimed = await _seed_child(conn, agent_id, parent_id, status="claimed")
        done = await _seed_child(conn, agent_id, parent_id, status="completed")
    try:
        async with await _client() as client:
            response = await client.post(f"/v1/runs/{parent_id}/cancel")
        assert response.status_code == 204

        statuses = await _statuses(engine, [queued, running, claimed, done])
        assert statuses[queued] == "cancelled"
        assert statuses[running] == "cancelled"
        assert statuses[claimed] == "cancelled"
        assert statuses[done] == "completed"  # untouched -- already terminal

        parent = await _statuses(engine, [parent_id])
        assert parent[parent_id] == "cancelled"
    finally:
        await _cleanup(engine, agent_id)


async def test_batch_cancel_closes_parent_immediately_in_one_transaction(
    engine: AsyncEngine,
) -> None:
    """No later child-side transition is needed to close the parent -- the
    cascade already made every child terminal by the time the response
    comes back."""
    async with engine.connect() as conn, conn.begin():
        agent_id = await _seed_agent(conn)
        parent_id = await _seed_parent(conn, agent_id)
        for _ in range(3):
            await _seed_child(conn, agent_id, parent_id, status="queued")
    try:
        async with await _client() as client:
            response = await client.post(f"/v1/runs/{parent_id}/cancel")
        assert response.status_code == 204

        async with engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT status, ended_at FROM runs WHERE id = :id"),
                        {"id": parent_id},
                    )
                )
                .mappings()
                .one()
            )
        assert row["status"] == "cancelled"
        assert row["ended_at"] is not None
    finally:
        await _cleanup(engine, agent_id)


async def test_batch_cancel_all_children_already_completed_closes_parent_completed(
    engine: AsyncEngine,
) -> None:
    """A batch parent that's still 'queued' (never claimed -- B3-03 excludes
    parents-with-children from claim.py's scan) but whose children already
    finished naturally (rollup raced ahead of a late-arriving cancel, or
    the caller double-clicked): cancel finds no live children to cascade
    to, and since none were ever cancelled the parent closes 'completed',
    not 'cancelled' -- the cascade only flips outcome for children still
    genuinely live at cancel time."""
    async with engine.connect() as conn, conn.begin():
        agent_id = await _seed_agent(conn)
        parent_id = await _seed_parent(conn, agent_id)
        await _seed_child(conn, agent_id, parent_id, status="completed")
        await _seed_child(conn, agent_id, parent_id, status="completed")
    try:
        async with await _client() as client:
            response = await client.post(f"/v1/runs/{parent_id}/cancel")
        assert response.status_code == 204

        async with engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT status FROM runs WHERE id = :id"), {"id": parent_id}
                    )
                )
                .mappings()
                .one()
            )
        assert row["status"] == "completed"
    finally:
        await _cleanup(engine, agent_id)


async def test_batch_cancel_already_terminal_parent_409s(engine: AsyncEngine) -> None:
    async with engine.connect() as conn, conn.begin():
        agent_id = await _seed_agent(conn)
        parent_id = await _seed_parent(conn, agent_id, status="completed")
    try:
        async with await _client() as client:
            response = await client.post(f"/v1/runs/{parent_id}/cancel")
        assert response.status_code == 409
    finally:
        await _cleanup(engine, agent_id)


async def test_batch_cancel_repeat_call_409s(engine: AsyncEngine) -> None:
    async with engine.connect() as conn, conn.begin():
        agent_id = await _seed_agent(conn)
        parent_id = await _seed_parent(conn, agent_id)
        await _seed_child(conn, agent_id, parent_id, status="queued")
    try:
        async with await _client() as client:
            first = await client.post(f"/v1/runs/{parent_id}/cancel")
            second = await client.post(f"/v1/runs/{parent_id}/cancel")
        assert first.status_code == 204
        assert second.status_code == 409
    finally:
        await _cleanup(engine, agent_id)


async def test_batch_cancel_missing_parent_404s() -> None:
    async with await _client() as client:
        response = await client.post(f"/v1/runs/{uuid.uuid4()}/cancel")
    assert response.status_code == 404
