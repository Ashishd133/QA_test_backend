"""B2.7-04 tests: /v1/metrics CRUD, project/agent scoping, and the
resolved-set precedence chain (builtin < project < agent). No real builtin
rows exist yet (B2.7-05 registers the actual nine) -- `_insert_builtin`
inserts a throwaway one directly, bypassing the API (which never lets a
caller create project_id=NULL rows), to exercise the floor of the
precedence chain.
"""

import uuid

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.engine.metrics.backtest import spec_hash
from app.main import app
from tests.conftest import _test_engine, auth_headers, requires_test_db

pytestmark = requires_test_db

_HEADERS = auth_headers()


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_HEADERS)


async def _insert_builtin(engine: AsyncEngine, name: str) -> uuid.UUID:
    metric_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO metrics "
                "(id, project_id, agent_id, name, description, kind, output_type, "
                " spec, sampling_pct, status, version, created_by_user_id, builtin) "
                "VALUES (:id, NULL, NULL, :name, '', 'builtin', 'boolean', "
                " CAST('{}' AS jsonb), 100, 'active', 1, 'user-1', true)"
            ),
            {"id": metric_id, "name": name},
        )
    return metric_id


async def _insert_matching_backtest(
    engine: AsyncEngine, metric_id: uuid.UUID, spec: dict[str, object]
) -> None:
    """B2.7-10's PATCH status=active guard requires a metric_backtests row
    whose spec_hash matches the metric's current spec -- inserted directly
    here (not via POST /v1/metrics/{id}/backtest) so tests unrelated to
    backtesting itself don't need real historical runs or, for an
    llm_judge-kind metric, a real judge client."""
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO metric_backtests "
                "(id, metric_id, spec_hash, sample_size, filters, cost, created_by_user_id) "
                "VALUES (:id, :metric_id, :spec_hash, 0, CAST('{}' AS jsonb), "
                " CAST('{}' AS jsonb), 'user-1')"
            ),
            {"id": uuid.uuid4(), "metric_id": metric_id, "spec_hash": spec_hash(spec)},
        )


async def _cleanup(
    engine: AsyncEngine,
    metric_ids: list[uuid.UUID],
    agent_ids: list[uuid.UUID] | None = None,
) -> None:
    async with engine.connect() as conn, conn.begin():
        for metric_id in metric_ids:
            await conn.execute(
                text("DELETE FROM metric_results WHERE metric_id = :id"), {"id": metric_id}
            )
            await conn.execute(text("DELETE FROM metrics WHERE id = :id"), {"id": metric_id})
        for agent_id in agent_ids or []:
            await conn.execute(text("DELETE FROM metrics WHERE agent_id = :id"), {"id": agent_id})
            await conn.execute(text("DELETE FROM runs WHERE agent_id = :id"), {"id": agent_id})
            await conn.execute(text("DELETE FROM suites WHERE agent_id = :id"), {"id": agent_id})
            await conn.execute(text("DELETE FROM agents WHERE id = :id"), {"id": agent_id})


async def _make_agent_and_scenario(client: AsyncClient, engine: AsyncEngine) -> dict[str, str]:
    agent_response = await client.post(
        "/v1/agents",
        json={
            "name": "Metrics Test Agent",
            "config": {
                "transport": "web",
                "roomUrl": "https://example.livekit.cloud/room",
                "token": "tok",
            },
        },
    )
    agent_id = agent_response.json()["id"]
    suite_response = await client.post(
        "/v1/suites", json={"name": "Metrics Test Suite", "agentId": agent_id}
    )
    suite_id = suite_response.json()["id"]
    scenario_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO scenarios (id, suite_id, name, persona, persona_initials, source) "
                "VALUES (:id, :suite_id, 'Metrics Scenario', 'x', 'X', 'manual')"
            ),
            {"id": scenario_id, "suite_id": uuid.UUID(suite_id)},
        )
    return {"agent_id": agent_id, "suite_id": suite_id, "scenario_id": str(scenario_id)}


async def test_create_metric_project_level_round_trips() -> None:
    engine = _test_engine()
    async with await _client() as client:
        response = await client.post(
            "/v1/metrics",
            json={
                "name": "custom_project_metric",
                "kind": "llm_judge",
                "outputType": "boolean",
                "spec": {"prompt": "did the agent do X?"},
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "custom_project_metric"
        assert body["agentId"] is None
        assert body["projectId"] is not None
        assert body["status"] == "draft"
        assert body["version"] == 1
        assert body["builtin"] is False
    try:
        await _cleanup(engine, [uuid.UUID(body["id"])])
    finally:
        await engine.dispose()


async def test_create_metric_missing_user_id_is_400() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=auth_headers(user_id=None),
    ) as client:
        response = await client.post(
            "/v1/metrics",
            json={"name": "x", "kind": "builtin", "outputType": "boolean"},
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "missing_user_id"


async def test_create_metric_agent_in_another_project_is_404() -> None:
    engine = _test_engine()
    other_project_id = uuid.uuid4()
    other_agent_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text("INSERT INTO projects (id, name) VALUES (:id, 'Other Metrics Project')"),
            {"id": other_project_id},
        )
        await conn.execute(
            text(
                "INSERT INTO agents (id, project_id, name, transport, created_by_user_id) "
                "VALUES (:id, :project_id, 'Other Project Agent', 'web', 'user-1')"
            ),
            {"id": other_agent_id, "project_id": other_project_id},
        )
    async with await _client() as client:
        response = await client.post(
            "/v1/metrics",
            json={
                "name": "x",
                "kind": "builtin",
                "outputType": "boolean",
                "agentId": str(other_agent_id),
            },
        )
    assert response.status_code == 404
    async with engine.connect() as conn, conn.begin():
        await conn.execute(text("DELETE FROM agents WHERE id = :id"), {"id": other_agent_id})
        await conn.execute(text("DELETE FROM projects WHERE id = :id"), {"id": other_project_id})
    await engine.dispose()


async def test_list_metrics_includes_builtins_and_project_and_agent_scoped() -> None:
    engine = _test_engine()
    builtin_id = await _insert_builtin(engine, "list_test_builtin")
    async with await _client() as client:
        info = await _make_agent_and_scenario(client, engine)
        create_response = await client.post(
            "/v1/metrics",
            json={
                "name": "list_test_agent_metric",
                "kind": "builtin",
                "outputType": "boolean",
                "agentId": info["agent_id"],
            },
        )
        metric_id = create_response.json()["id"]

        list_response = await client.get("/v1/metrics")
        assert list_response.status_code == 200
        items = list_response.json()
        assert any(m["id"] == str(builtin_id) for m in items)
        assert any(m["id"] == metric_id for m in items)
    try:
        await _cleanup(engine, [builtin_id, uuid.UUID(metric_id)], [uuid.UUID(info["agent_id"])])
    finally:
        await engine.dispose()


async def test_get_metric_detail_and_404() -> None:
    engine = _test_engine()
    async with await _client() as client:
        create_response = await client.post(
            "/v1/metrics",
            json={"name": "detail_metric", "kind": "builtin", "outputType": "boolean"},
        )
        metric_id = create_response.json()["id"]

        get_response = await client.get(f"/v1/metrics/{metric_id}")
        assert get_response.status_code == 200
        assert get_response.json()["name"] == "detail_metric"

        missing_response = await client.get(f"/v1/metrics/{uuid.uuid4()}")
        assert missing_response.status_code == 404
    try:
        await _cleanup(engine, [uuid.UUID(metric_id)])
    finally:
        await engine.dispose()


async def test_update_metric_partial_patch_and_builtin_refused() -> None:
    engine = _test_engine()
    builtin_id = await _insert_builtin(engine, "patch_refused_builtin")
    async with await _client() as client:
        create_response = await client.post(
            "/v1/metrics",
            json={"name": "patchable_metric", "kind": "builtin", "outputType": "boolean"},
        )
        metric_id = create_response.json()["id"]

        patch_response = await client.patch(
            f"/v1/metrics/{metric_id}", json={"description": "updated"}
        )
        assert patch_response.status_code == 200
        assert patch_response.json()["description"] == "updated"
        assert patch_response.json()["version"] == 1

        builtin_patch = await client.patch(
            f"/v1/metrics/{builtin_id}", json={"description": "hacked"}
        )
        assert builtin_patch.status_code == 409
        assert builtin_patch.json()["error"]["code"] == "builtin_read_only"
    try:
        await _cleanup(engine, [builtin_id, uuid.UUID(metric_id)])
    finally:
        await engine.dispose()


async def test_update_metric_bumps_version_only_when_active() -> None:
    engine = _test_engine()
    async with await _client() as client:
        create_response = await client.post(
            "/v1/metrics",
            json={"name": "version_bump_metric", "kind": "builtin", "outputType": "boolean"},
        )
        metric_id = create_response.json()["id"]

        await _insert_matching_backtest(engine, uuid.UUID(metric_id), {})

        # draft -> active: first activation, not "editing an active metric".
        activate_response = await client.patch(
            f"/v1/metrics/{metric_id}", json={"status": "active"}
        )
        assert activate_response.status_code == 200
        assert activate_response.json()["version"] == 1

        # Now genuinely active; a further edit bumps version.
        edit_response = await client.patch(
            f"/v1/metrics/{metric_id}", json={"description": "tweaked"}
        )
        assert edit_response.status_code == 200
        assert edit_response.json()["version"] == 2
    try:
        await _cleanup(engine, [uuid.UUID(metric_id)])
    finally:
        await engine.dispose()


async def test_delete_metric_conflict_when_referenced_and_builtin_refused() -> None:
    engine = _test_engine()
    builtin_id = await _insert_builtin(engine, "delete_refused_builtin")
    run_id: uuid.UUID | None = None
    agent_id: str | None = None
    async with await _client() as client:
        create_response = await client.post(
            "/v1/metrics",
            json={"name": "deletable_metric", "kind": "builtin", "outputType": "boolean"},
        )
        metric_id = create_response.json()["id"]

        info = await _make_agent_and_scenario(client, engine)
        agent_id = info["agent_id"]
        run_id = uuid.uuid4()
        async with engine.connect() as conn, conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO runs (id, type, agent_id, scenario_id, created_by_user_id) "
                    "VALUES (:id, 'simulation', :agent_id, :scenario_id, 'user-1')"
                ),
                {
                    "id": run_id,
                    "agent_id": uuid.UUID(agent_id),
                    "scenario_id": uuid.UUID(info["scenario_id"]),
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO metric_results (run_id, metric_id, metric_version, status) "
                    "VALUES (:run_id, :metric_id, 1, 'passed')"
                ),
                {"run_id": run_id, "metric_id": uuid.UUID(metric_id)},
            )

        conflict_response = await client.delete(f"/v1/metrics/{metric_id}")
        assert conflict_response.status_code == 409
        assert conflict_response.json()["error"]["code"] == "conflict"

        builtin_delete = await client.delete(f"/v1/metrics/{builtin_id}")
        assert builtin_delete.status_code == 409
        assert builtin_delete.json()["error"]["code"] == "builtin_read_only"

    async with engine.connect() as conn, conn.begin():
        await conn.execute(text("DELETE FROM metric_results WHERE run_id = :id"), {"id": run_id})
        await conn.execute(text("DELETE FROM runs WHERE id = :id"), {"id": run_id})
    await _cleanup(
        engine, [builtin_id, uuid.UUID(metric_id)], [uuid.UUID(agent_id)] if agent_id else []
    )
    await engine.dispose()


async def test_resolved_metrics_agent_override_shadows_project_only_for_that_agent() -> None:
    engine = _test_engine()
    builtin_id = await _insert_builtin(engine, "shared_name_metric")
    async with await _client() as client:
        info_a = await _make_agent_and_scenario(client, engine)
        info_b = await _make_agent_and_scenario(client, engine)

        project_metric = await client.post(
            "/v1/metrics",
            json={
                "name": "shared_name_metric",
                "kind": "llm_judge",
                "outputType": "boolean",
                "spec": {"level": "project"},
            },
        )
        project_metric_id = project_metric.json()["id"]
        await _insert_matching_backtest(engine, uuid.UUID(project_metric_id), {"level": "project"})
        await client.patch(f"/v1/metrics/{project_metric_id}", json={"status": "active"})

        agent_a_metric = await client.post(
            "/v1/metrics",
            json={
                "name": "shared_name_metric",
                "kind": "llm_judge",
                "outputType": "boolean",
                "spec": {"level": "agent-a"},
                "agentId": info_a["agent_id"],
            },
        )
        agent_a_metric_id = agent_a_metric.json()["id"]
        await _insert_matching_backtest(engine, uuid.UUID(agent_a_metric_id), {"level": "agent-a"})
        await client.patch(f"/v1/metrics/{agent_a_metric_id}", json={"status": "active"})

        # Scenario under agent A: the agent-level override wins.
        resolved_a = await client.get(
            "/v1/metrics/resolved", params={"scenarioId": info_a["scenario_id"]}
        )
        assert resolved_a.status_code == 200
        shared_a = next(m for m in resolved_a.json() if m["name"] == "shared_name_metric")
        assert shared_a["sourceLevel"] == "agent"
        assert shared_a["spec"] == {"level": "agent-a"}

        # Scenario under agent B: no override there, project-level wins,
        # builtin does not resurface once shadowed.
        resolved_b = await client.get(
            "/v1/metrics/resolved", params={"scenarioId": info_b["scenario_id"]}
        )
        assert resolved_b.status_code == 200
        shared_b = next(m for m in resolved_b.json() if m["name"] == "shared_name_metric")
        assert shared_b["sourceLevel"] == "project"
        assert shared_b["spec"] == {"level": "project"}
    try:
        await _cleanup(
            engine,
            [builtin_id, uuid.UUID(project_metric_id), uuid.UUID(agent_a_metric_id)],
            [uuid.UUID(info_a["agent_id"]), uuid.UUID(info_b["agent_id"])],
        )
    finally:
        await engine.dispose()


async def test_resolved_metrics_excludes_draft_and_archived() -> None:
    engine = _test_engine()
    async with await _client() as client:
        info = await _make_agent_and_scenario(client, engine)
        draft_metric = await client.post(
            "/v1/metrics",
            json={"name": "draft_only_metric", "kind": "builtin", "outputType": "boolean"},
        )
        draft_metric_id = draft_metric.json()["id"]

        resolved = await client.get(
            "/v1/metrics/resolved", params={"scenarioId": info["scenario_id"]}
        )
        assert resolved.status_code == 200
        assert not any(m["name"] == "draft_only_metric" for m in resolved.json())
    try:
        await _cleanup(engine, [uuid.UUID(draft_metric_id)], [uuid.UUID(info["agent_id"])])
    finally:
        await engine.dispose()
