"""B2.5-03 tests: Run/Call vocabulary at the API surface.

A Test Run is a row with parent_run_id IS NULL; a Call has it set.
`?isParent=true` / `?parentRunId=` filter GET /v1/runs; GET /v1/runs/{id}
embeds a read-computed `aggregate` for any Test Run (batch or, per the
ticket's "do not special-case them" rule, a single implicit-call run).

The property test seeds N random batches directly via SQL (no engine
executor needed -- B2.6 is what actually runs batches) and asserts the
API's `aggregate` matches an independent Python reduction over the same
rows, proving the read-computed aggregate can't drift from what
`?parentRunId=` itself would show for the children.
"""

import json
import random
import uuid
from collections import Counter
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.main import app
from app.verdict import verdict_for_run
from tests.conftest import _test_engine, auth_headers, requires_test_db

pytestmark = requires_test_db

_HEADERS = auth_headers()

_STATUSES = ("queued", "running", "completed", "completed", "completed", "cancelled", "failed")
_BADGES = ("pass", "pass", "warn", "fail")
_PERSONAS = ("Priya", "Alex", "Sam")


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


async def _make_suite_and_scenarios(
    engine: AsyncEngine, agent_id: uuid.UUID, n: int
) -> list[tuple[uuid.UUID, str]]:
    """Returns (scenario_id, persona) pairs -- the caller already knows
    each scenario's persona from this, so seeding a batch never needs a
    per-child SELECT back against `scenarios` (see this module's docstring
    on why every DB round trip here is budgeted: `null_pool=True` pays a
    fresh connection-establishment cost on every single `.connect()`,
    independent of query count, against this environment's slow test DB)."""
    suite_id = uuid.uuid4()
    scenario_ids = [uuid.uuid4() for _ in range(n)]
    personas = [random.choice(_PERSONAS) for _ in range(n)]
    values_sql = ", ".join(
        f"(:id{i}, :suite_id, 'Scenario', :persona{i}, 'PR', 'manual')" for i in range(n)
    )
    params: dict[str, Any] = {"suite_id": suite_id}
    for i, (sid, persona) in enumerate(zip(scenario_ids, personas, strict=True)):
        params[f"id{i}"] = sid
        params[f"persona{i}"] = persona
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO suites (id, name, agent_id, created_by_user_id) "
                "VALUES (:id, 'Aggregate Suite', :agent_id, 'user-1')"
            ),
            {"id": suite_id, "agent_id": agent_id},
        )
        await conn.execute(
            text(
                "INSERT INTO scenarios "
                "(id, suite_id, name, persona, persona_initials, source) VALUES " + values_sql
            ),
            params,
        )
    return list(zip(scenario_ids, personas, strict=True))


async def _cleanup(engine: AsyncEngine, agent_id: uuid.UUID) -> None:
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text("DELETE FROM turns WHERE run_id IN (SELECT id FROM runs WHERE agent_id = :id)"),
            {"id": agent_id},
        )
        await conn.execute(text("DELETE FROM runs WHERE agent_id = :id"), {"id": agent_id})
        await conn.execute(text("DELETE FROM suites WHERE agent_id = :id"), {"id": agent_id})
        await conn.execute(text("DELETE FROM agents WHERE id = :id"), {"id": agent_id})
    await engine.dispose()


def _reduce_expected(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Independent (non-app-code) reduction over the same fixture rows,
    for the property test to compare the API's aggregate against."""
    status_counts = Counter(r["status"] for r in rows)
    verdicts = [verdict_for_run(r["status"], r["metrics"]) for r in rows]
    scored = [v for v in verdicts if v != "idle"]
    pass_rate = (sum(1 for v in scored if v == "pass") / len(scored)) if scored else None
    total_cost = sum(r["cost"]["estUsd"] for r in rows if r.get("cost") and "estUsd" in r["cost"])
    scenario_ids = {r["scenario_id"] for r in rows if r["scenario_id"] is not None}
    personas = {r["persona"] for r in rows if r.get("persona")}
    return {
        "callCount": len(rows),
        "statusCounts": dict(status_counts),
        "passRate": pass_rate,
        "totalCostUsd": round(total_cost, 6) if total_cost else None,
        "distinctScenarioCount": len(scenario_ids),
        "distinctPersonaCount": len(personas),
    }


async def _seed_batch(
    engine: AsyncEngine,
    agent_id: uuid.UUID,
    scenario_persona: list[tuple[uuid.UUID, str]],
    n_children: int,
) -> tuple[uuid.UUID, list[dict[str, Any]]]:
    """One multi-row INSERT for the parent + all children -- one
    `.connect()`, one round trip, regardless of `n_children` (see
    `_make_suite_and_scenarios`'s docstring)."""
    parent_id = uuid.uuid4()
    rows: list[dict[str, Any]] = []
    values_sql_parts = [
        "(:parent_id, 'suite', 'completed', :agent_id, NULL, NULL, NULL, NULL, 'user-1')"
    ]
    params: dict[str, Any] = {"parent_id": parent_id, "agent_id": agent_id}

    for i in range(n_children):
        child_id = uuid.uuid4()
        run_status = random.choice(_STATUSES)
        scenario_id, persona = random.choice(scenario_persona)
        metrics: dict[str, Any] | None = None
        if run_status == "completed":
            metrics = {"resultBadge": random.choice(_BADGES), "score": random.uniform(0, 1)}
        cost = {
            "llmInputTokens": 10,
            "llmOutputTokens": 10,
            "judgeCalls": 1,
            "sttSeconds": 1.0,
            "ttsChars": 10,
            "estUsd": round(random.uniform(0.001, 0.5), 4),
        }
        values_sql_parts.append(
            f"(:id{i}, 'simulation', :status{i}, :agent_id, :scenario{i}, :parent_id, "
            f"CAST(:metrics{i} AS jsonb), CAST(:cost{i} AS jsonb), 'user-1')"
        )
        params.update(
            {
                f"id{i}": child_id,
                f"status{i}": run_status,
                f"scenario{i}": scenario_id,
                f"metrics{i}": _dump(metrics),
                f"cost{i}": _dump(cost),
            }
        )
        rows.append(
            {
                "id": child_id,
                "status": run_status,
                "metrics": metrics,
                "cost": cost,
                "scenario_id": scenario_id,
                "persona": persona,
            }
        )

    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO runs "
                "(id, type, status, agent_id, scenario_id, parent_run_id, metrics, cost, "
                " created_by_user_id) VALUES " + ", ".join(values_sql_parts)
            ),
            params,
        )
    return parent_id, rows


def _dump(value: dict[str, Any] | None) -> str | None:
    return json.dumps(value) if value is not None else None


async def test_isparent_and_parent_run_id_filters() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine, "Filter Agent")
    scenario_persona = await _make_suite_and_scenarios(engine, agent_id, 3)
    parent_id, rows = await _seed_batch(engine, agent_id, scenario_persona, 5)
    try:
        async with await _client() as client:
            parents_resp = await client.get("/v1/runs", params={"isParent": "true"})
            children_resp = await client.get("/v1/runs", params={"parentRunId": str(parent_id)})
        parent_ids = {r["id"] for r in parents_resp.json()}
        assert str(parent_id) in parent_ids
        assert not any(r["id"] in {str(row["id"]) for row in rows} for r in parents_resp.json())

        child_ids = {r["id"] for r in children_resp.json()}
        assert child_ids == {str(row["id"]) for row in rows}
        assert all(not r["isParent"] for r in children_resp.json())
    finally:
        await _cleanup(engine, agent_id)


async def test_legacy_single_run_gets_self_aggregate() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine, "Legacy Single Run Agent")
    async with engine.connect() as conn, conn.begin():
        run_id = (
            await conn.execute(
                text(
                    "INSERT INTO runs (id, type, status, agent_id, metrics, created_by_user_id) "
                    "VALUES (:id, 'simulation', 'completed', :agent_id, "
                    " CAST(:metrics AS jsonb), 'user-1') RETURNING id"
                ),
                {"id": uuid.uuid4(), "agent_id": agent_id, "metrics": '{"resultBadge": "pass"}'},
            )
        ).scalar_one()
    try:
        async with await _client() as client:
            response = await client.get(f"/v1/runs/{run_id}")
        assert response.status_code == 200
        body = response.json()
        assert body["isParent"] is True
        assert body["parentRunId"] is None
        assert body["aggregate"]["callCount"] == 1
        assert body["aggregate"]["statusCounts"] == {"completed": 1}
        assert body["aggregate"]["passRate"] == 1.0
    finally:
        await _cleanup(engine, agent_id)


async def test_call_detail_has_no_aggregate() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine, "Call Detail Agent")
    scenario_persona = await _make_suite_and_scenarios(engine, agent_id, 1)
    parent_id, rows = await _seed_batch(engine, agent_id, scenario_persona, 1)
    try:
        async with await _client() as client:
            response = await client.get(f"/v1/runs/{rows[0]['id']}")
        assert response.status_code == 200
        body = response.json()
        assert body["isParent"] is False
        assert body["parentRunId"] == str(parent_id)
        assert body["aggregate"] is None
    finally:
        await _cleanup(engine, agent_id)


@pytest.mark.timeout(900)
async def test_parent_aggregate_matches_independent_reduce_over_children() -> None:
    """The ticket's named property test, run for 20 generated batches.

    20 iterations x (1 seed connect + 1 HTTP-call connect) is inherently
    ~40 fresh connection establishments -- `null_pool=True` (see
    tests/conftest.py) never reuses one, and this environment's Neon test
    branch has measured ~12s per establishment. The default 120s
    per-test timeout is sized for normal tests, not this one; bumped here
    rather than raised globally.
    """
    engine = _test_engine()
    agent_id = await _make_agent(engine, "Property Test Agent")
    scenario_persona = await _make_suite_and_scenarios(engine, agent_id, 4)
    try:
        for _ in range(20):
            n_children = random.randint(1, 8)
            parent_id, rows = await _seed_batch(engine, agent_id, scenario_persona, n_children)
            expected = _reduce_expected(rows)

            async with await _client() as client:
                response = await client.get(f"/v1/runs/{parent_id}")
            assert response.status_code == 200
            aggregate = response.json()["aggregate"]

            assert aggregate["callCount"] == expected["callCount"]
            assert aggregate["statusCounts"] == expected["statusCounts"]
            if expected["passRate"] is None:
                assert aggregate["passRate"] is None
            else:
                assert aggregate["passRate"] == pytest_approx(expected["passRate"])
            assert aggregate["distinctScenarioCount"] == expected["distinctScenarioCount"]
            assert aggregate["distinctPersonaCount"] == expected["distinctPersonaCount"]
            if expected["totalCostUsd"] is None:
                assert aggregate["totalCostUsd"] is None
            else:
                assert aggregate["totalCostUsd"] == pytest_approx(expected["totalCostUsd"])
    finally:
        await _cleanup(engine, agent_id)


def pytest_approx(value: float, tol: float = 1e-6) -> Any:
    return pytest.approx(value, abs=tol)
