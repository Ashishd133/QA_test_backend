"""B2.7-10 tests: POST /v1/metrics/{id}/backtest + the PATCH status=active
`backtest_required` guard. Uses a `builtin`-kind metric (response_latency)
for the main flow -- no real Gemini call needed, unlike an `llm_judge`
metric; that dispatch path is already exercised at the unit level by
tests/test_metric_compilation.py's fake-client tests, so this file
doesn't burn a real API call re-proving the same thing end to end.
"""

import uuid

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.main import app
from tests.conftest import _test_engine, auth_headers, requires_test_db
from tests.test_metrics import _make_agent_and_scenario

pytestmark = requires_test_db

_HEADERS = auth_headers()


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_HEADERS)


async def _seed_completed_run_with_turns(
    engine: AsyncEngine, agent_id: str, scenario_id: str
) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO runs (id, type, status, agent_id, scenario_id, created_by_user_id) "
                "VALUES (:id, 'simulation', 'completed', :agent_id, :scenario_id, 'user-1')"
            ),
            {"id": run_id, "agent_id": uuid.UUID(agent_id), "scenario_id": uuid.UUID(scenario_id)},
        )
        await conn.execute(
            text(
                "INSERT INTO turns (run_id, idx, role, text, latency_ms) "
                "VALUES (:run_id, 0, 'caller', 'I need help.', NULL), "
                "       (:run_id, 1, 'agent', 'Sure, one moment.', 1200)"
            ),
            {"run_id": run_id},
        )
    return run_id


async def _cleanup(
    engine: AsyncEngine, agent_id: str, metric_ids: list[uuid.UUID], run_id: uuid.UUID | None
) -> None:
    async with engine.connect() as conn, conn.begin():
        if run_id is not None:
            await conn.execute(text("DELETE FROM turns WHERE run_id = :id"), {"id": run_id})
            await conn.execute(text("DELETE FROM runs WHERE id = :id"), {"id": run_id})
        for metric_id in metric_ids:
            await conn.execute(
                text("DELETE FROM metric_backtests WHERE metric_id = :id"), {"id": metric_id}
            )
            await conn.execute(text("DELETE FROM metrics WHERE id = :id"), {"id": metric_id})
        await conn.execute(text("DELETE FROM runs WHERE agent_id = :id"), {"id": agent_id})
        await conn.execute(
            text(
                "DELETE FROM scenarios WHERE suite_id IN "
                "(SELECT id FROM suites WHERE agent_id = :id)"
            ),
            {"id": agent_id},
        )
        await conn.execute(text("DELETE FROM suites WHERE agent_id = :id"), {"id": agent_id})
        await conn.execute(text("DELETE FROM agents WHERE id = :id"), {"id": agent_id})


async def test_backtest_returns_a_verdict_per_historical_call_and_a_cost() -> None:
    engine = _test_engine()
    run_id: uuid.UUID | None = None
    async with await _client() as client:
        info = await _make_agent_and_scenario(client, engine)
        run_id = await _seed_completed_run_with_turns(engine, info["agent_id"], info["scenario_id"])
        create_response = await client.post(
            "/v1/metrics",
            json={
                "name": "response_latency",
                "kind": "builtin",
                "outputType": "numeric",
                "spec": {"threshold": 3000},
                "agentId": info["agent_id"],
            },
        )
        metric_id = create_response.json()["id"]

        backtest_response = await client.post(
            f"/v1/metrics/{metric_id}/backtest",
            json={"sampleSize": 5, "filters": {"agentId": info["agent_id"]}},
        )
        assert backtest_response.status_code == 201
        body = backtest_response.json()
        assert len(body["verdicts"]) == 1
        assert body["verdicts"][0]["runId"] == str(run_id)
        assert body["verdicts"][0]["status"] == "passed"  # 1200ms <= 3000ms threshold
        assert body["agreement"] is None
        assert body["labeledCount"] == 0
        assert "cost" in body
    try:
        await _cleanup(engine, info["agent_id"], [uuid.UUID(metric_id)], run_id)
    finally:
        await engine.dispose()


async def test_patch_status_active_without_a_backtest_is_422() -> None:
    engine = _test_engine()
    async with await _client() as client:
        info = await _make_agent_and_scenario(client, engine)
        create_response = await client.post(
            "/v1/metrics",
            json={
                "name": "talk_ratio",
                "kind": "builtin",
                "outputType": "numeric",
                "agentId": info["agent_id"],
            },
        )
        metric_id = create_response.json()["id"]

        patch_response = await client.patch(f"/v1/metrics/{metric_id}", json={"status": "active"})
        assert patch_response.status_code == 422
        assert patch_response.json()["error"]["code"] == "backtest_required"
    try:
        await _cleanup(engine, info["agent_id"], [uuid.UUID(metric_id)], None)
    finally:
        await engine.dispose()


async def test_patch_status_active_after_a_matching_backtest_succeeds() -> None:
    engine = _test_engine()
    run_id: uuid.UUID | None = None
    async with await _client() as client:
        info = await _make_agent_and_scenario(client, engine)
        run_id = await _seed_completed_run_with_turns(engine, info["agent_id"], info["scenario_id"])
        create_response = await client.post(
            "/v1/metrics",
            json={
                "name": "response_latency",
                "kind": "builtin",
                "outputType": "numeric",
                "agentId": info["agent_id"],
            },
        )
        metric_id = create_response.json()["id"]

        await client.post(
            f"/v1/metrics/{metric_id}/backtest",
            json={"sampleSize": 5, "filters": {"agentId": info["agent_id"]}},
        )

        patch_response = await client.patch(f"/v1/metrics/{metric_id}", json={"status": "active"})
        assert patch_response.status_code == 200
        assert patch_response.json()["status"] == "active"
    try:
        await _cleanup(engine, info["agent_id"], [uuid.UUID(metric_id)], run_id)
    finally:
        await engine.dispose()


async def test_backtest_with_no_historical_calls_returns_empty_verdicts() -> None:
    engine = _test_engine()
    async with await _client() as client:
        info = await _make_agent_and_scenario(client, engine)
        create_response = await client.post(
            "/v1/metrics",
            json={
                "name": "response_latency",
                "kind": "builtin",
                "outputType": "numeric",
                "agentId": info["agent_id"],
            },
        )
        metric_id = create_response.json()["id"]

        backtest_response = await client.post(
            f"/v1/metrics/{metric_id}/backtest",
            json={"sampleSize": 5, "filters": {"agentId": info["agent_id"]}},
        )
        assert backtest_response.status_code == 201
        assert backtest_response.json()["verdicts"] == []
    try:
        await _cleanup(engine, info["agent_id"], [uuid.UUID(metric_id)], None)
    finally:
        await engine.dispose()
