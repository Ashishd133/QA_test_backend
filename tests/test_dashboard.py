"""B1-06 tests: GET /v1/metrics/dashboard and GET /v1/personas.

The ticket's acceptance bar is "numbers reconcile with a seeded fixture
set" -- this seeds runs at explicit created_at offsets (current week,
previous week, and older-than-14-days-to-prove-exclusion) and asserts
every DashboardMetrics field against a hand-computed expectation.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import get_settings
from app.main import app
from tests.conftest import _test_engine, requires_test_db

pytestmark = requires_test_db

_HEADERS = {"Authorization": f"Bearer {get_settings().python_service_token}"}


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_HEADERS)


async def _make_agent(engine: AsyncEngine) -> uuid.UUID:
    agent_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO agents (id, name, transport, created_by_user_id) "
                "VALUES (:id, 'Dashboard Agent', 'web', 'user-1')"
            ),
            {"id": agent_id},
        )
    return agent_id


async def _make_suite_and_scenarios(engine: AsyncEngine, agent_id: uuid.UUID) -> list[uuid.UUID]:
    suite_id = uuid.uuid4()
    scenario_ids = [uuid.uuid4(), uuid.uuid4()]
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO suites (id, name, agent_id, created_by_user_id) "
                "VALUES (:id, 'Dashboard Suite', :agent_id, 'user-1')"
            ),
            {"id": suite_id, "agent_id": agent_id},
        )
        for i, scenario_id in enumerate(scenario_ids):
            await conn.execute(
                text(
                    "INSERT INTO scenarios "
                    "(id, suite_id, name, persona, persona_initials, source) "
                    "VALUES (:id, :suite_id, :name, 'Priya', 'PR', 'manual')"
                ),
                {"id": scenario_id, "suite_id": suite_id, "name": f"Scenario {i}"},
            )
    return scenario_ids


async def _make_run(
    engine: AsyncEngine,
    agent_id: uuid.UUID,
    *,
    days_ago: float,
    status: str,
    scenario_id: uuid.UUID | None = None,
    result_badge: str | None = None,
    avg_latency_ms: float | None = None,
) -> None:
    created_at = datetime.now(UTC) - timedelta(days=days_ago)
    metrics: dict[str, object] = {}
    if result_badge is not None:
        metrics["resultBadge"] = result_badge
    if avg_latency_ms is not None:
        metrics["avgLatencyMs"] = avg_latency_ms
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO runs "
                "(id, type, status, agent_id, scenario_id, created_at, metrics, "
                " created_by_user_id) "
                "VALUES (:id, 'simulation', :status, :agent_id, :scenario_id, :created_at, "
                " CAST(:metrics AS jsonb), 'user-1')"
            ),
            {
                "id": uuid.uuid4(),
                "status": status,
                "agent_id": agent_id,
                "scenario_id": scenario_id,
                "created_at": created_at,
                "metrics": json.dumps(metrics) if metrics else None,
            },
        )


async def _cleanup(engine: AsyncEngine, agent_id: uuid.UUID) -> None:
    async with engine.connect() as conn, conn.begin():
        await conn.execute(text("DELETE FROM runs WHERE agent_id = :id"), {"id": agent_id})
        await conn.execute(text("DELETE FROM suites WHERE agent_id = :id"), {"id": agent_id})
        await conn.execute(text("DELETE FROM agents WHERE id = :id"), {"id": agent_id})
    await engine.dispose()


async def test_dashboard_metrics_reconcile_with_seeded_fixture() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine)
    scenario1, scenario2 = await _make_suite_and_scenarios(engine, agent_id)

    try:
        # Current week (last 7 days): 4 runs -- 2 pass, 1 fail, 1 still queued.
        await _make_run(
            engine,
            agent_id,
            days_ago=1,
            status="completed",
            scenario_id=scenario1,
            result_badge="pass",
            avg_latency_ms=400,
        )
        await _make_run(
            engine,
            agent_id,
            days_ago=2,
            status="completed",
            result_badge="pass",
            avg_latency_ms=600,
        )
        await _make_run(
            engine,
            agent_id,
            days_ago=3,
            status="completed",
            scenario_id=scenario1,
            result_badge="fail",
            avg_latency_ms=800,
        )
        await _make_run(engine, agent_id, days_ago=4, status="queued")

        # Previous week (8-14 days ago): 2 runs -- 1 pass, 1 fail.
        await _make_run(
            engine,
            agent_id,
            days_ago=9,
            status="completed",
            scenario_id=scenario2,
            result_badge="pass",
            avg_latency_ms=1000,
        )
        await _make_run(
            engine,
            agent_id,
            days_ago=10,
            status="completed",
            result_badge="fail",
            avg_latency_ms=1200,
        )

        # Older than 14 days -- must be excluded entirely.
        await _make_run(engine, agent_id, days_ago=20, status="completed", result_badge="pass")

        async with await _client() as client:
            response = await client.get("/v1/metrics/dashboard")
        assert response.status_code == 200
        body = response.json()

        assert body["testRuns7d"] == "4"
        assert body["testRunsDelta"] == "+2"

        assert body["passRate"] == "67%"
        assert body["passRateDelta"] == "+17%"

        assert body["avgLatency"] == "600ms"
        assert body["avgLatencyDelta"] == "-500ms"

        assert body["scenarioCoverage"] == "100%"
        assert body["scenarioCoverageDelta"] == "+50%"

        assert sum(body["trendBars"]) == 6
        assert len(body["trendBars"]) == 14

        outcome = body["outcome"]
        assert outcome == {"passed": 2, "warnings": 0, "failed": 1, "passPct": 67}
    finally:
        await _cleanup(engine, agent_id)


async def test_dashboard_metrics_well_formed_regardless_of_other_data() -> None:
    """No agent scoping on this endpoint (it's a global dashboard), so this
    only checks shape/types -- asserting exact counts here would make the
    test depend on every other test file's cleanup having run cleanly."""
    async with await _client() as client:
        response = await client.get("/v1/metrics/dashboard")
    assert response.status_code == 200
    body = response.json()
    assert body["testRuns7d"].isdigit()
    assert len(body["trendBars"]) == 14
    assert 0 <= body["outcome"]["passPct"] <= 100


async def test_list_personas_returns_seeded_builtins() -> None:
    engine = _test_engine()
    persona_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO personas (id, name, voice, language, builtin) "
                "VALUES (:id, 'Aggressive Caller', 'alloy', 'en', true)"
            ),
            {"id": persona_id},
        )
    try:
        async with await _client() as client:
            response = await client.get("/v1/personas")
        assert response.status_code == 200
        item = next(p for p in response.json() if p["id"] == str(persona_id))
        assert item["name"] == "Aggressive Caller"
        assert item["builtin"] is True
    finally:
        async with engine.connect() as conn, conn.begin():
            await conn.execute(text("DELETE FROM personas WHERE id = :id"), {"id": persona_id})
