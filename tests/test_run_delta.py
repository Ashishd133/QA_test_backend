"""B2.5-05 tests: GET /v1/runs?parentRunId=&since=&fields=light.

The endpoint's own docstring rule (app/api/runs.py's _run_delta): this is
the one polling path for a call queue -- no per-row SSE. Covers the
row-shape contract, the since= filter, and the ETag/If-None-Match -> 304
short-circuit (migration 007's trigger is what makes `updated_at`, and
therefore the ETag, trustworthy).
"""

import json
import uuid

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.main import app
from tests.conftest import _test_engine, auth_headers, requires_test_db

pytestmark = requires_test_db

_HEADERS = auth_headers()


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_HEADERS)


async def _make_agent(engine: AsyncEngine, name: str) -> uuid.UUID:
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


async def _make_parent(engine: AsyncEngine, agent_id: uuid.UUID) -> uuid.UUID:
    async with engine.connect() as conn, conn.begin():
        parent_id = (
            await conn.execute(
                text(
                    "INSERT INTO runs (id, type, status, agent_id, created_by_user_id) "
                    "VALUES (:id, 'suite', 'completed', :agent_id, 'user-1') RETURNING id"
                ),
                {"id": uuid.uuid4(), "agent_id": agent_id},
            )
        ).scalar_one()
    return uuid.UUID(str(parent_id))


async def _make_child(
    engine: AsyncEngine, agent_id: uuid.UUID, parent_id: uuid.UUID, *, status: str = "completed"
) -> uuid.UUID:
    async with engine.connect() as conn, conn.begin():
        child_id = (
            await conn.execute(
                text(
                    "INSERT INTO runs "
                    "(id, type, status, agent_id, parent_run_id, metrics, created_by_user_id) "
                    "VALUES (:id, 'simulation', :status, :agent_id, :parent_id, "
                    " CAST(:metrics AS jsonb), 'user-1') RETURNING id"
                ),
                {
                    "id": uuid.uuid4(),
                    "status": status,
                    "agent_id": agent_id,
                    "parent_id": parent_id,
                    "metrics": json.dumps({"resultBadge": "pass", "score": 0.9}),
                },
            )
        ).scalar_one()
    return uuid.UUID(str(child_id))


async def _cleanup(engine: AsyncEngine, agent_id: uuid.UUID) -> None:
    async with engine.connect() as conn, conn.begin():
        await conn.execute(text("DELETE FROM runs WHERE agent_id = :id"), {"id": agent_id})
        await conn.execute(text("DELETE FROM agents WHERE id = :id"), {"id": agent_id})
    await engine.dispose()


async def test_delta_requires_parent_run_id() -> None:
    async with await _client() as client:
        response = await client.get("/v1/runs", params={"fields": "light"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_delta_returns_light_rows_and_aggregate() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine, "Delta Agent")
    parent_id = await _make_parent(engine, agent_id)
    child_id = await _make_child(engine, agent_id, parent_id)
    try:
        async with await _client() as client:
            response = await client.get(
                "/v1/runs", params={"parentRunId": str(parent_id), "fields": "light"}
            )
        assert response.status_code == 200
        assert "etag" in {k.lower() for k in response.headers}
        body = response.json()
        assert {row["id"] for row in body["calls"]} == {str(child_id)}
        row = body["calls"][0]
        assert set(row.keys()) == {
            "id",
            "status",
            "score",
            "turns",
            "durationMs",
            "latencyP95",
            "goalMet",
        }
        assert row["score"] == 0.9
        assert row["goalMet"] is None
        assert body["aggregate"]["callCount"] == 1
    finally:
        await _cleanup(engine, agent_id)


async def test_delta_unchanged_returns_304_without_calls_key() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine, "Delta 304 Agent")
    parent_id = await _make_parent(engine, agent_id)
    await _make_child(engine, agent_id, parent_id)
    try:
        async with await _client() as client:
            first = await client.get(
                "/v1/runs", params={"parentRunId": str(parent_id), "fields": "light"}
            )
        etag = first.headers["etag"]

        async with await _client() as client:
            second = await client.get(
                "/v1/runs",
                params={"parentRunId": str(parent_id), "fields": "light"},
                headers={"If-None-Match": etag},
            )
        assert second.status_code == 304
        assert second.content == b""
    finally:
        await _cleanup(engine, agent_id)


async def test_delta_changed_after_new_child_returns_fresh_etag() -> None:
    """Proves migration 007's trigger actually moves updated_at -- without
    it this test 304s twice, silently hiding a new call from the queue."""
    engine = _test_engine()
    agent_id = await _make_agent(engine, "Delta Change Agent")
    parent_id = await _make_parent(engine, agent_id)
    await _make_child(engine, agent_id, parent_id)
    try:
        async with await _client() as client:
            first = await client.get(
                "/v1/runs", params={"parentRunId": str(parent_id), "fields": "light"}
            )
        etag = first.headers["etag"]

        await _make_child(engine, agent_id, parent_id, status="queued")

        async with await _client() as client:
            second = await client.get(
                "/v1/runs",
                params={"parentRunId": str(parent_id), "fields": "light"},
                headers={"If-None-Match": etag},
            )
        assert second.status_code == 200
        assert len(second.json()["calls"]) == 2
        assert second.headers["etag"] != etag
    finally:
        await _cleanup(engine, agent_id)


async def test_delta_since_filters_unchanged_rows() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine, "Delta Since Agent")
    parent_id = await _make_parent(engine, agent_id)
    await _make_child(engine, agent_id, parent_id)
    try:
        async with await _client() as client:
            baseline = await client.get(
                "/v1/runs", params={"parentRunId": str(parent_id), "fields": "light"}
            )
        assert len(baseline.json()["calls"]) == 1

        async with await _client() as client:
            future = await client.get(
                "/v1/runs",
                params={
                    "parentRunId": str(parent_id),
                    "fields": "light",
                    "since": "2999-01-01T00:00:00Z",
                },
            )
        assert future.status_code == 200
        assert future.json()["calls"] == []
        # aggregate reflects the whole batch regardless of the since cursor.
        assert future.json()["aggregate"]["callCount"] == 1
    finally:
        await _cleanup(engine, agent_id)


async def test_delta_rejects_call_id_as_parent_run_id() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine, "Delta Call As Parent Agent")
    parent_id = await _make_parent(engine, agent_id)
    child_id = await _make_child(engine, agent_id, parent_id)
    try:
        async with await _client() as client:
            response = await client.get(
                "/v1/runs", params={"parentRunId": str(child_id), "fields": "light"}
            )
        assert response.status_code == 422
    finally:
        await _cleanup(engine, agent_id)
