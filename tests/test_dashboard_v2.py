"""B2.8-01/02/03/04 tests: the v2 additions to GET /v1/metrics/dashboard
and the new GET /v1/metrics/failing-checks, against a dedicated project so
counts reconcile exactly (see test_dashboard.py's module docstring for why
that matters -- the shared default project accumulates other test files'
leftover rows).

v1's own fields (pass rate, trend bars, outcome) are covered by
test_dashboard.py; this file only covers what B2.8 actually added.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.main import app
from tests.conftest import _test_engine, auth_headers, builtin_persona_id, requires_test_db

pytestmark = requires_test_db


async def _client(project_id: uuid.UUID) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=auth_headers(project_id=project_id),
    )


async def _make_project_and_agent(engine: AsyncEngine) -> tuple[uuid.UUID, uuid.UUID]:
    project_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text("INSERT INTO projects (id, name) VALUES (:id, 'Dashboard v2 Test Project')"),
            {"id": project_id},
        )
        await conn.execute(
            text(
                "INSERT INTO agents (id, project_id, name, transport, created_by_user_id) "
                "VALUES (:id, :project_id, 'v2 Agent', 'web', 'user-1')"
            ),
            {"id": agent_id, "project_id": project_id},
        )
    return project_id, agent_id


async def _make_leaf_run(
    engine: AsyncEngine,
    *,
    project_id: uuid.UUID,
    agent_id: uuid.UUID,
    status: str = "completed",
    result_badge: str | None = "pass",
    cost_usd: float | None = None,
    scenario_id: uuid.UUID | None = None,
    days_ago: float = 0,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    metrics = {"resultBadge": result_badge} if result_badge else None
    cost = {"estUsd": cost_usd} if cost_usd is not None else None
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO runs "
                "(id, type, status, project_id, agent_id, scenario_id, created_at, metrics, "
                " cost, created_by_user_id) "
                "VALUES (:id, 'simulation', :status, :project_id, :agent_id, :scenario_id, "
                " :created_at, CAST(:metrics AS jsonb), CAST(:cost AS jsonb), 'user-1')"
            ),
            {
                "id": run_id,
                "status": status,
                "project_id": project_id,
                "agent_id": agent_id,
                "scenario_id": scenario_id,
                "created_at": datetime.now(UTC) - timedelta(days=days_ago),
                "metrics": json.dumps(metrics) if metrics else None,
                "cost": json.dumps(cost) if cost else None,
            },
        )
    return run_id


async def _cleanup(engine: AsyncEngine, project_id: uuid.UUID) -> None:
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "DELETE FROM findings WHERE run_id IN (SELECT id FROM runs WHERE project_id = :id)"
            ),
            {"id": project_id},
        )
        await conn.execute(
            text("DELETE FROM turns WHERE run_id IN (SELECT id FROM runs WHERE project_id = :id)"),
            {"id": project_id},
        )
        await conn.execute(
            text(
                "DELETE FROM assertion_results WHERE run_id IN "
                "(SELECT id FROM runs WHERE project_id = :id)"
            ),
            {"id": project_id},
        )
        await conn.execute(
            text(
                "DELETE FROM metric_results WHERE run_id IN "
                "(SELECT id FROM runs WHERE project_id = :id)"
            ),
            {"id": project_id},
        )
        await conn.execute(text("DELETE FROM metrics WHERE project_id = :id"), {"id": project_id})
        await conn.execute(text("DELETE FROM runs WHERE project_id = :id"), {"id": project_id})
        await conn.execute(
            text(
                "DELETE FROM scenarios WHERE suite_id IN "
                "(SELECT id FROM suites WHERE project_id = :id)"
            ),
            {"id": project_id},
        )
        await conn.execute(text("DELETE FROM suites WHERE project_id = :id"), {"id": project_id})
        await conn.execute(text("DELETE FROM agents WHERE project_id = :id"), {"id": project_id})
        await conn.execute(text("DELETE FROM projects WHERE id = :id"), {"id": project_id})
    await engine.dispose()


async def test_posture_open_critical_findings_and_latency_percentiles() -> None:
    engine = _test_engine()
    project_id, agent_id = await _make_project_and_agent(engine)
    try:
        run_id = await _make_leaf_run(
            engine, project_id=project_id, agent_id=agent_id, cost_usd=0.05
        )
        async with engine.connect() as conn, conn.begin():
            for idx, latency_ms in enumerate([100, 500, 900, 1900, 2500]):
                await conn.execute(
                    text(
                        "INSERT INTO turns (run_id, idx, role, text, latency_ms) "
                        "VALUES (:run_id, :idx, 'agent', 'hi', :latency_ms)"
                    ),
                    {"run_id": run_id, "idx": idx, "latency_ms": latency_ms},
                )
            # Open critical (counted), resolved critical (not counted, per
            # `resolved_at IS NOT NULL`), open non-critical (not counted).
            await conn.execute(
                text(
                    "INSERT INTO findings "
                    "(id, run_id, category, severity, verdict, attack_prompt, agent_response, "
                    " evidence, suggested_fix) "
                    "VALUES (:id, :run_id, 'pii_exfiltration', 'critical', 'leaked', 'p', 'r', "
                    " 'evidence', 'fix')"
                ),
                {"id": uuid.uuid4(), "run_id": run_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO findings "
                    "(id, run_id, category, severity, verdict, attack_prompt, agent_response, "
                    " evidence, suggested_fix, resolved_at) "
                    "VALUES (:id, :run_id, 'auth_bypass', 'critical', 'leaked', 'p', 'r', "
                    " 'evidence', 'fix', now())"
                ),
                {"id": uuid.uuid4(), "run_id": run_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO findings "
                    "(id, run_id, category, severity, verdict, attack_prompt, agent_response, "
                    " suggested_fix) "
                    "VALUES (:id, :run_id, 'jailbreak', 'medium', 'blocked', 'p', 'r', 'fix')"
                ),
                {"id": uuid.uuid4(), "run_id": run_id},
            )

        async with await _client(project_id) as client:
            response = await client.get("/v1/metrics/dashboard")
        assert response.status_code == 200
        body = response.json()

        assert body["posture"]["openCriticalFindings"] == 1
        assert body["posture"]["openCriticalFindingsByCategory"] == {"pii_exfiltration": 1}

        latency = body["latency"]
        assert latency["totalTurns"] == 5
        assert latency["flaggedTurns"] == 2  # >1000ms: 1900, 2500
        assert latency["p50Ms"] == pytest.approx(900.0)
        assert latency["p90Ms"] == pytest.approx(2260.0)
        assert latency["p95Ms"] == pytest.approx(2380.0)
        # width_bucket(v, 0, 2400, 12): 100->1, 500->3, 900->5, 1900->10,
        # 2500 overflows (>=2400) and is clamped into the last bucket.
        assert latency["histogram"] == [1, 0, 1, 0, 1, 0, 0, 0, 0, 1, 0, 1]
        assert body["posture"]["latencyP95Ms"] == pytest.approx(2380.0)
    finally:
        await _cleanup(engine, project_id)


async def test_spend_and_credits_remaining() -> None:
    engine = _test_engine()
    project_id, agent_id = await _make_project_and_agent(engine)
    try:
        await _make_leaf_run(engine, project_id=project_id, agent_id=agent_id, cost_usd=2.0)
        await _make_leaf_run(engine, project_id=project_id, agent_id=agent_id, cost_usd=3.0)

        async with await _client(project_id) as client:
            no_budget = (await client.get("/v1/metrics/dashboard")).json()
        assert no_budget["spend"] == {
            "costPerCall": 2.5,
            "windowSpend": 5.0,
            "creditsRemainingPct": None,
        }

        async with engine.connect() as conn, conn.begin():
            await conn.execute(
                text("UPDATE projects SET budget_usd_monthly = 100 WHERE id = :id"),
                {"id": project_id},
            )
        async with await _client(project_id) as client:
            with_budget = (await client.get("/v1/metrics/dashboard")).json()
        assert with_budget["spend"]["creditsRemainingPct"] == pytest.approx(95.0)

        async with engine.connect() as conn, conn.begin():
            await conn.execute(
                text("UPDATE projects SET budget_usd_monthly = 0 WHERE id = :id"),
                {"id": project_id},
            )
        async with await _client(project_id) as client:
            zero_budget = (await client.get("/v1/metrics/dashboard")).json()
        assert zero_budget["spend"]["creditsRemainingPct"] == 0.0
    finally:
        await _cleanup(engine, project_id)


async def test_recent_runs_single_and_batch() -> None:
    engine = _test_engine()
    project_id, agent_id = await _make_project_and_agent(engine)
    suite_id = uuid.uuid4()
    scenario_ids = [uuid.uuid4(), uuid.uuid4()]
    try:
        persona_id = await builtin_persona_id(engine)
        async with engine.connect() as conn, conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO suites (id, project_id, name, agent_id, created_by_user_id) "
                    "VALUES (:id, :project_id, 'v2 Suite', :agent_id, 'user-1')"
                ),
                {"id": suite_id, "project_id": project_id, "agent_id": agent_id},
            )
            for i, scenario_id in enumerate(scenario_ids):
                await conn.execute(
                    text(
                        "INSERT INTO scenarios (id, suite_id, name, persona_id, source) "
                        "VALUES (:id, :suite_id, :name, :persona_id, 'manual')"
                    ),
                    {
                        "id": scenario_id,
                        "suite_id": suite_id,
                        "name": f"v2 Scenario {i}",
                        "persona_id": persona_id,
                    },
                )

        # Childless single run -- its own implicit call.
        await _make_leaf_run(
            engine,
            project_id=project_id,
            agent_id=agent_id,
            scenario_id=scenario_ids[0],
            result_badge="pass",
            days_ago=0.01,
        )

        # Batch: parent (rolled-up 'fail' per rollup.py) + 2 children.
        parent_id = uuid.uuid4()
        async with engine.connect() as conn, conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO runs "
                    "(id, type, status, project_id, agent_id, created_at, metrics, "
                    " created_by_user_id) "
                    "VALUES (:id, 'suite', 'completed', :project_id, :agent_id, now(), "
                    " CAST('{\"resultBadge\": \"fail\"}' AS jsonb), 'user-1')"
                ),
                {"id": parent_id, "project_id": project_id, "agent_id": agent_id},
            )
            for scenario_id, badge in zip(scenario_ids, ["pass", "fail"], strict=True):
                await conn.execute(
                    text(
                        "INSERT INTO runs "
                        "(id, type, status, project_id, agent_id, scenario_id, parent_run_id, "
                        " created_at, metrics, created_by_user_id) "
                        "VALUES (:id, 'suite', 'completed', :project_id, :agent_id, "
                        " :scenario_id, :parent_id, now(), CAST(:metrics AS jsonb), 'user-1')"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "project_id": project_id,
                        "agent_id": agent_id,
                        "scenario_id": scenario_id,
                        "parent_id": parent_id,
                        "metrics": json.dumps({"resultBadge": badge}),
                    },
                )

        async with await _client(project_id) as client:
            response = await client.get("/v1/metrics/dashboard")
        recent = {r["id"]: r for r in response.json()["recentRuns"]}

        batch = recent[str(parent_id)]
        assert batch["name"] == "v2 Suite"
        assert batch["callCount"] == 2
        assert batch["passRate"] == pytest.approx(0.5)
        # The parent's own rolled-up verdict, not recomputed from children.
        assert batch["status"] == "fail"

        single = next(r for r in recent.values() if r["id"] != str(parent_id))
        assert single["name"] == "v2 Scenario 0"
        assert single["callCount"] == 1
        assert single["passRate"] == pytest.approx(1.0)
        assert single["status"] == "pass"
    finally:
        await _cleanup(engine, project_id)


async def test_failing_checks_ranked_by_fail_count() -> None:
    engine = _test_engine()
    project_id, agent_id = await _make_project_and_agent(engine)
    metric_id = uuid.uuid4()
    try:
        call_a = await _make_leaf_run(engine, project_id=project_id, agent_id=agent_id)
        call_b = await _make_leaf_run(engine, project_id=project_id, agent_id=agent_id)
        async with engine.connect() as conn, conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO metrics "
                    "(id, project_id, name, kind, output_type, created_by_user_id) "
                    "VALUES (:id, :project_id, 'Politeness', 'llm_judge', 'boolean', 'user-1')"
                ),
                {"id": metric_id, "project_id": project_id},
            )
            for run_id, assertion_id, status in [
                (call_a, "a1", "failed"),
                (call_b, "a1", "passed"),
                (call_a, "a2", "failed"),
                (call_b, "a2", "failed"),
            ]:
                await conn.execute(
                    text(
                        "INSERT INTO assertion_results (run_id, assertion_id, name, status) "
                        "VALUES (:run_id, :assertion_id, :name, :status)"
                    ),
                    {
                        "run_id": run_id,
                        "assertion_id": assertion_id,
                        "name": f"Check {assertion_id}",
                        "status": status,
                    },
                )
            await conn.execute(
                text(
                    "INSERT INTO metric_results (run_id, metric_id, metric_version, status) "
                    "VALUES (:run_id, :metric_id, 1, 'failed')"
                ),
                {"run_id": call_a, "metric_id": metric_id},
            )

        async with await _client(project_id) as client:
            response = await client.get("/v1/metrics/failing-checks")
        assert response.status_code == 200
        checks = {c["checkId"]: c for c in response.json()}

        assert checks["a2"]["kind"] == "assertion"
        assert checks["a2"]["failCount"] == 2
        assert checks["a2"]["totalCount"] == 2
        assert checks["a2"]["ratePct"] == 100
        assert checks["a2"]["drilldownFilter"] == "failingCheckKind=assertion&failingCheckId=a2"

        assert checks["a1"]["failCount"] == 1
        assert checks["a1"]["totalCount"] == 2
        assert checks["a1"]["ratePct"] == 50

        assert checks[str(metric_id)]["kind"] == "metric"
        assert checks[str(metric_id)]["failCount"] == 1
        assert checks[str(metric_id)]["totalCount"] == 1
        assert checks[str(metric_id)]["name"] == "Politeness"

        # a2 (2 fails) ranks above the two 1-fail checks.
        assert response.json()[0]["checkId"] == "a2"
    finally:
        await _cleanup(engine, project_id)
