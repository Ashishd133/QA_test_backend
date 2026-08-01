"""B1-05 tests: POST /v1/runs/{id}/cancel and /rerun.

The core acceptance bar -- "cancelling mid-FakeRunner-stream yields
status:cancelled + partial results in detail" -- is driven end-to-end:
a real run_fake_script() task is started, cancelled mid-script via the
actual HTTP endpoint, then GET /v1/runs/{id} is checked for partial data.
"""

import asyncio
import json
import uuid

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import get_settings
from app.main import app
from app.workers.claim import claim_run
from app.workers.fake_runner import run_fake_script
from tests.conftest import _test_engine, requires_test_db

pytestmark = requires_test_db

_HEADERS = {
    "Authorization": f"Bearer {get_settings().python_service_token}",
    "X-User-Id": "user-1",
}


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_HEADERS)


async def _make_agent(engine: AsyncEngine, *, max_concurrency: int = 5) -> uuid.UUID:
    agent_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO agents (id, name, transport, max_concurrency, created_by_user_id) "
                "VALUES (:id, 'Lifecycle Agent', 'web', :max_concurrency, 'user-1')"
            ),
            {"id": agent_id, "max_concurrency": max_concurrency},
        )
    return agent_id


async def _make_queued_run(engine: AsyncEngine, agent_id: uuid.UUID) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO runs (id, type, agent_id, created_by_user_id) "
                "VALUES (:id, 'simulation', :agent_id, 'user-1')"
            ),
            {"id": run_id, "agent_id": agent_id},
        )
    return run_id


async def _cleanup(engine: AsyncEngine, agent_id: uuid.UUID) -> None:
    async with engine.connect() as conn, conn.begin():
        # B1-07 materializes turns/assertion_results on `done` (no cascade
        # from runs -- they're derived data, not the run_events truth), so
        # a completed/cancelled run leaves rows here that must go first.
        await conn.execute(
            text("DELETE FROM turns WHERE run_id IN (SELECT id FROM runs WHERE agent_id = :id)"),
            {"id": agent_id},
        )
        await conn.execute(
            text(
                "DELETE FROM assertion_results "
                "WHERE run_id IN (SELECT id FROM runs WHERE agent_id = :id)"
            ),
            {"id": agent_id},
        )
        await conn.execute(
            text(
                "DELETE FROM run_events WHERE run_id IN (SELECT id FROM runs WHERE agent_id = :id)"
            ),
            {"id": agent_id},
        )
        await conn.execute(text("DELETE FROM runs WHERE agent_id = :id"), {"id": agent_id})
        await conn.execute(text("DELETE FROM suites WHERE agent_id = :id"), {"id": agent_id})
        await conn.execute(text("DELETE FROM agents WHERE id = :id"), {"id": agent_id})


async def test_cancel_mid_script_yields_cancelled_status_and_partial_results() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine)
    await _make_queued_run(engine, agent_id)
    try:
        claimed = await claim_run(engine, "test-worker-cancel")
        assert claimed is not None
        run_task = asyncio.create_task(run_fake_script(engine, claimed))

        # Poll for at least one event rather than guess a fixed sleep --
        # connection-establishment latency before the script's own delays
        # even start is unpredictable enough that a fixed sleep raced the
        # cancel ahead of the first turn once already.
        for _ in range(40):
            async with engine.connect() as conn:
                event_count = (
                    await conn.execute(
                        text("SELECT count(*) FROM run_events WHERE run_id = :id"),
                        {"id": claimed.id},
                    )
                ).scalar_one()
            if event_count >= 2:
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("script never emitted events within the poll window")

        async with await _client() as client:
            cancel_response = await client.post(f"/v1/runs/{claimed.id}/cancel")
        assert cancel_response.status_code == 204

        await asyncio.wait_for(run_task, timeout=15.0)

        async with engine.connect() as conn:
            status_row = (
                await conn.execute(
                    text("SELECT status FROM runs WHERE id = :id"), {"id": claimed.id}
                )
            ).scalar_one()
        assert status_row == "cancelled"

        async with await _client() as client:
            detail_response = await client.get(f"/v1/runs/{claimed.id}")
        assert detail_response.status_code == 200
        body = detail_response.json()
        assert body["status"] == "fail"
        assert 0 < len(body["transcript"]) < 4
    finally:
        await _cleanup(engine, agent_id)


async def test_cancel_unknown_run_is_404() -> None:
    async with await _client() as client:
        response = await client.post(f"/v1/runs/{uuid.uuid4()}/cancel")
    assert response.status_code == 404


async def test_cancel_already_completed_run_is_409() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine)
    async with engine.connect() as conn, conn.begin():
        run_id = (
            await conn.execute(
                text(
                    "INSERT INTO runs (id, type, status, agent_id, created_by_user_id) "
                    "VALUES (:id, 'simulation', 'completed', :agent_id, 'user-1') RETURNING id"
                ),
                {"id": uuid.uuid4(), "agent_id": agent_id},
            )
        ).scalar_one()
    try:
        async with await _client() as client:
            response = await client.post(f"/v1/runs/{run_id}/cancel")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "conflict"
    finally:
        await _cleanup(engine, agent_id)


async def test_cancel_requires_user_id() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine)
    run_id = await _make_queued_run(engine, agent_id)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {get_settings().python_service_token}"},
        ) as client:
            response = await client.post(f"/v1/runs/{run_id}/cancel")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "missing_user_id"
    finally:
        await _cleanup(engine, agent_id)


async def test_rerun_clones_config_into_new_queued_run() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine)
    async with engine.connect() as conn, conn.begin():
        original_id = (
            await conn.execute(
                text(
                    "INSERT INTO runs (id, type, status, agent_id, config, created_by_user_id) "
                    "VALUES (:id, 'redteam', 'failed', :agent_id, "
                    "CAST(:config AS jsonb), 'user-1') RETURNING id"
                ),
                {
                    "id": uuid.uuid4(),
                    "agent_id": agent_id,
                    "config": json.dumps({"categories": ["PII"]}),
                },
            )
        ).scalar_one()
    try:
        async with await _client() as client:
            response = await client.post(f"/v1/runs/{original_id}/rerun")
        assert response.status_code == 202
        new_run_id = response.json()["runId"]
        assert new_run_id != str(original_id)

        async with engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT type, status, agent_id, config FROM runs WHERE id = :id"),
                        {"id": uuid.UUID(new_run_id)},
                    )
                )
                .mappings()
                .one()
            )
        assert row["type"] == "redteam"
        assert row["status"] == "queued"
        assert row["agent_id"] == agent_id
        assert row["config"]["categories"] == ["PII"]
    finally:
        await _cleanup(engine, agent_id)


async def test_rerun_unknown_run_is_404() -> None:
    async with await _client() as client:
        response = await client.post(f"/v1/runs/{uuid.uuid4()}/rerun")
    assert response.status_code == 404


async def test_rerun_requires_user_id() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine)
    run_id = await _make_queued_run(engine, agent_id)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {get_settings().python_service_token}"},
        ) as client:
            response = await client.post(f"/v1/runs/{run_id}/rerun")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "missing_user_id"
    finally:
        await _cleanup(engine, agent_id)
