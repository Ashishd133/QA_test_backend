"""B1-03 tests: GET /v1/runs (list) and GET /v1/runs/{id} (detail).

Detail is reduced live from run_events, not the turns/assertion_results
tables B1-07 later materializes -- the ticket's own acceptance bar is that a
FakeRunner-completed run's detail matches the frontend Run type, so this
drives an actual FakeRunner execution rather than hand-seeding events.
"""

import uuid

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import get_settings
from app.main import app
from app.models.projects import DEFAULT_PROJECT_ID
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


async def _make_agent(engine: AsyncEngine, name: str = "Runs Read Agent") -> uuid.UUID:
    agent_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO agents (id, name, transport, created_by_user_id) "
                "VALUES (:id, :name, 'web', 'user-1')"
            ),
            {"id": agent_id, "name": name},
        )
    return agent_id


async def _cleanup(engine: AsyncEngine, agent_id: uuid.UUID) -> None:
    async with engine.connect() as conn, conn.begin():
        # B1-07 materializes turns/assertion_results on `done` (no cascade
        # from runs -- they're derived data, not the run_events truth), so
        # a completed run leaves rows here that must go before `runs` does.
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
    await engine.dispose()


async def test_run_detail_matches_frontend_run_shape_after_fake_runner_completes() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine)
    async with engine.connect() as conn, conn.begin():
        run_id = (
            await conn.execute(
                text(
                    "INSERT INTO runs (id, type, agent_id, created_by_user_id) "
                    "VALUES (:id, 'simulation', :agent_id, 'user-1') RETURNING id"
                ),
                {"id": uuid.uuid4(), "agent_id": agent_id},
            )
        ).scalar_one()

    try:
        claimed = await claim_run(engine, "test-worker-runs-read")
        assert claimed is not None
        await run_fake_script(engine, claimed)

        async with await _client() as client:
            response = await client.get(f"/v1/runs/{run_id}")
        assert response.status_code == 200
        body = response.json()

        assert body["title"]
        assert body["agentName"] == "Runs Read Agent"
        assert body["status"] == "pass"
        assert body["score"] == 95
        assert body["avgLatency"] == "700ms"
        assert body["wer"] == "-"
        assert body["sentiment"] == "-"
        assert body["duration"] != "-"

        # B2-06: projectId defaults to the seeded project (server_default,
        # not left null) since nothing set it explicitly; endReason/cost
        # stay null until B2-08's executor writes them.
        assert body["projectId"] == str(DEFAULT_PROJECT_ID)
        assert body["endReason"] is None
        assert body["cost"] is None

        transcript = body["transcript"]
        assert len(transcript) == 4
        assert transcript[0]["role"] == "caller"
        assert transcript[1]["lat"] == "420ms"

        result_assertions = body["resultAssertions"]
        assert len(result_assertions) == 2
        assert all(a["ok"] for a in result_assertions)

        assert body["latencySeries"] == [420.0, 980.0]
    finally:
        await _cleanup(engine, agent_id)


async def test_run_detail_404_for_missing_run() -> None:
    async with await _client() as client:
        response = await client.get(f"/v1/runs/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_list_runs_filters_by_status_and_agent() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine, "List Runs Agent")
    other_agent_id = await _make_agent(engine, "Other Agent")
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
        await conn.execute(
            text(
                "INSERT INTO runs (id, type, status, agent_id, created_by_user_id) "
                "VALUES (:id, 'simulation', 'queued', :other_agent_id, 'user-1')"
            ),
            {"id": uuid.uuid4(), "other_agent_id": other_agent_id},
        )

    try:
        async with await _client() as client:
            response = await client.get(
                "/v1/runs", params={"agentId": str(agent_id), "status": "completed"}
            )
        assert response.status_code == 200
        ids = [r["id"] for r in response.json()]
        assert str(run_id) in ids

        async with await _client() as client:
            response_other = await client.get(
                "/v1/runs", params={"agentId": str(other_agent_id), "status": "completed"}
            )
        assert response_other.json() == []
    finally:
        await _cleanup(engine, agent_id)
        await _cleanup(engine, other_agent_id)
