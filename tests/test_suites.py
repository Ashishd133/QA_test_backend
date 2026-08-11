"""B1-01 tests: /v1/suites and /v1/scenarios against the live Neon DB.

Response shapes are pinned to the frontend's src/types/index.ts Suite/
Scenario interfaces (provided directly -- CADENCE_API_ARCHITECTURE.md wasn't
available). `status`/`pr`/`passRate` are computed from each scenario's most
recent run, not stored columns -- see app/api/suites.py's docstrings for the
exact verdict-folding rules being asserted here.
"""

import json
import uuid

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import get_settings
from app.main import app
from tests.conftest import _test_engine, auth_headers, requires_test_db

pytestmark = requires_test_db

_HEADERS = auth_headers()


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_HEADERS)


async def _make_agent(engine: AsyncEngine, name: str = "Test Agent") -> uuid.UUID:
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


async def _make_suite(
    engine: AsyncEngine, agent_id: uuid.UUID, name: str = "Test Suite"
) -> uuid.UUID:
    suite_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO suites (id, name, description, agent_id, created_by_user_id) "
                "VALUES (:id, :name, 'desc', :agent_id, 'user-1')"
            ),
            {"id": suite_id, "name": name, "agent_id": agent_id},
        )
    return suite_id


async def _make_scenario(
    engine: AsyncEngine, suite_id: uuid.UUID, name: str = "Test Scenario"
) -> uuid.UUID:
    scenario_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO scenarios "
                "(id, suite_id, name, persona, persona_initials, assertions, source) "
                "VALUES (:id, :suite_id, :name, 'Priya', 'PR', CAST(:assertions AS jsonb), "
                "'manual')"
            ),
            {
                "id": scenario_id,
                "suite_id": suite_id,
                "name": name,
                "assertions": json.dumps([{"id": "a1"}, {"id": "a2"}]),
            },
        )
    return scenario_id


async def _make_completed_run(
    engine: AsyncEngine,
    agent_id: uuid.UUID,
    scenario_id: uuid.UUID,
    *,
    status: str = "completed",
    score: float = 0.95,
    result_badge: str = "pass",
) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO runs "
                "(id, type, status, agent_id, scenario_id, created_by_user_id, metrics) "
                "VALUES (:id, 'simulation', :status, :agent_id, :scenario_id, 'user-1', "
                "CAST(:metrics AS jsonb))"
            ),
            {
                "id": run_id,
                "status": status,
                "agent_id": agent_id,
                "scenario_id": scenario_id,
                "metrics": json.dumps({"score": score, "resultBadge": result_badge}),
            },
        )
    return run_id


async def _cleanup(engine: AsyncEngine, *, agent_id: uuid.UUID) -> None:
    async with engine.connect() as conn, conn.begin():
        await conn.execute(text("DELETE FROM runs WHERE agent_id = :id"), {"id": agent_id})
        await conn.execute(text("DELETE FROM suites WHERE agent_id = :id"), {"id": agent_id})
        await conn.execute(text("DELETE FROM agents WHERE id = :id"), {"id": agent_id})
    await engine.dispose()


async def test_list_suites_computes_count_and_idle_verdict() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine, "List Agent")
    suite_id = await _make_suite(engine, agent_id, "List Suite")
    await _make_scenario(engine, suite_id, "Never Run Scenario")

    try:
        async with await _client() as client:
            response = await client.get("/v1/suites")
        assert response.status_code == 200
        body = next(s for s in response.json() if s["id"] == str(suite_id))
        assert body["agent"] == "List Agent"
        assert body["count"] == 1
        assert body["pr"] == 0
        assert body["passRate"] == "0%"
        assert body["lastRun"] == "Never"
    finally:
        await _cleanup(engine, agent_id=agent_id)


async def test_suite_detail_embeds_scenarios_with_computed_status() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine, "Detail Agent")
    suite_id = await _make_suite(engine, agent_id, "Detail Suite")
    scenario_id = await _make_scenario(engine, suite_id, "Passing Scenario")
    run_id = await _make_completed_run(
        engine, agent_id, scenario_id, score=0.95, result_badge="pass"
    )

    try:
        async with await _client() as client:
            response = await client.get(f"/v1/suites/{suite_id}")
        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 1
        assert body["pr"] == 100
        assert body["passRate"] == "100%"
        assert len(body["scenarios"]) == 1
        scenario = body["scenarios"][0]
        assert scenario["id"] == str(scenario_id)
        assert scenario["assertCount"] == 2
        assert scenario["status"] == "pass"
        assert scenario["score"] == "95%"
        assert scenario["runId"] == str(run_id)
    finally:
        await _cleanup(engine, agent_id=agent_id)


async def test_suite_detail_404_for_missing_suite() -> None:
    async with await _client() as client:
        response = await client.get(f"/v1/suites/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_failed_run_yields_fail_verdict() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine, "Fail Agent")
    suite_id = await _make_suite(engine, agent_id, "Fail Suite")
    scenario_id = await _make_scenario(engine, suite_id, "Failing Scenario")
    await _make_completed_run(engine, agent_id, scenario_id, status="failed", score=0.2)

    try:
        async with await _client() as client:
            response = await client.get(f"/v1/suites/{suite_id}")
        assert response.status_code == 200
        scenario = response.json()["scenarios"][0]
        assert scenario["status"] == "fail"
    finally:
        await _cleanup(engine, agent_id=agent_id)


async def test_create_suite_requires_user_id() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine, "Create Agent")
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {get_settings().python_service_token}"},
        ) as client:
            response = await client.post(
                "/v1/suites", json={"name": "New Suite", "agentId": str(agent_id)}
            )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "missing_user_id"
    finally:
        await _cleanup(engine, agent_id=agent_id)


async def test_create_suite_unknown_agent_is_404() -> None:
    async with await _client() as client:
        response = await client.post(
            "/v1/suites", json={"name": "Orphan Suite", "agentId": str(uuid.uuid4())}
        )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_create_suite_bad_agent_id_format_is_422() -> None:
    async with await _client() as client:
        response = await client.post(
            "/v1/suites", json={"name": "Bad Suite", "agentId": "not-a-uuid"}
        )
    assert response.status_code == 422


async def test_create_and_delete_suite_roundtrip() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine, "Roundtrip Agent")
    try:
        async with await _client() as client:
            create_response = await client.post(
                "/v1/suites", json={"name": "Roundtrip Suite", "agentId": str(agent_id)}
            )
            assert create_response.status_code == 201
            body = create_response.json()
            assert body["agent"] == "Roundtrip Agent"
            assert body["lastRun"] == "Never"
            suite_id = body["id"]

            delete_response = await client.delete(f"/v1/suites/{suite_id}")
            assert delete_response.status_code == 204

            second_delete = await client.delete(f"/v1/suites/{suite_id}")
            assert second_delete.status_code == 404
    finally:
        await _cleanup(engine, agent_id=agent_id)


async def test_update_suite_partial_patch() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine, "Patch Agent")
    suite_id = await _make_suite(engine, agent_id, "Original Name")
    try:
        async with await _client() as client:
            response = await client.patch(f"/v1/suites/{suite_id}", json={"name": "Renamed"})
        assert response.status_code == 200
        assert response.json()["name"] == "Renamed"
        assert response.json()["desc"] == "desc"
    finally:
        await _cleanup(engine, agent_id=agent_id)


async def test_delete_suite_cascades_scenarios_and_preserves_run_history() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine, "Cascade Agent")
    suite_id = await _make_suite(engine, agent_id, "Cascade Suite")
    scenario_id = await _make_scenario(engine, suite_id)
    run_id = await _make_completed_run(engine, agent_id, scenario_id)

    try:
        async with await _client() as client:
            response = await client.delete(f"/v1/suites/{suite_id}")
        assert response.status_code == 204

        async with engine.connect() as conn:
            scenario_row = (
                await conn.execute(
                    text("SELECT 1 FROM scenarios WHERE id = :id"), {"id": scenario_id}
                )
            ).first()
            assert scenario_row is None

            run_row = (
                (
                    await conn.execute(
                        text("SELECT scenario_id FROM runs WHERE id = :id"), {"id": run_id}
                    )
                )
                .mappings()
                .one()
            )
            assert run_row["scenario_id"] is None
    finally:
        await _cleanup(engine, agent_id=agent_id)


async def test_add_scenario_manual_requires_name_and_persona() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine, "Manual Agent")
    suite_id = await _make_suite(engine, agent_id, "Manual Suite")
    try:
        async with await _client() as client:
            response = await client.post(f"/v1/suites/{suite_id}/scenarios", json={})
        assert response.status_code == 422
    finally:
        await _cleanup(engine, agent_id=agent_id)


async def test_add_scenario_manual_success() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine, "Manual Success Agent")
    suite_id = await _make_suite(engine, agent_id, "Manual Success Suite")
    try:
        async with await _client() as client:
            response = await client.post(
                f"/v1/suites/{suite_id}/scenarios",
                json={"name": "New Scenario", "persona": "Aggressive Caller"},
            )
        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "New Scenario"
        assert body["status"] == "idle"
        assert body["score"] == "-"
        assert body["runId"] == ""
    finally:
        await _cleanup(engine, agent_id=agent_id)


async def test_add_scenario_from_draft_is_idempotent() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine, "Draft Agent")
    suite_id = await _make_suite(engine, agent_id, "Draft Suite")
    run_id = uuid.uuid4()
    draft_id = f"draft-{uuid.uuid4()}"

    async with engine.connect() as conn, conn.begin():
        # status='completed' explicitly: this run only exists to satisfy
        # discovery_drafts.run_id's FK, and the live worker (polling this
        # same DB for status='queued') would otherwise claim and execute it
        # out from under the test before cleanup runs.
        await conn.execute(
            text(
                "INSERT INTO runs (id, type, status, agent_id, created_by_user_id) "
                "VALUES (:id, 'discovery', 'completed', :agent_id, 'user-1')"
            ),
            {"id": run_id, "agent_id": agent_id},
        )
        await conn.execute(
            text(
                "INSERT INTO discovery_drafts (run_id, draft_id, name, persona) "
                "VALUES (:run_id, :draft_id, 'Drafted Scenario', 'Skeptical Caller')"
            ),
            {"run_id": run_id, "draft_id": draft_id},
        )

    try:
        async with await _client() as client:
            first = await client.post(
                f"/v1/suites/{suite_id}/scenarios", json={"fromDraftId": draft_id}
            )
            assert first.status_code == 201
            first_body = first.json()
            assert first_body["name"] == "Drafted Scenario"

            second = await client.post(
                f"/v1/suites/{suite_id}/scenarios", json={"fromDraftId": draft_id}
            )
            assert second.status_code == 200
            assert second.json()["id"] == first_body["id"]

        async with engine.connect() as conn:
            count = (
                await conn.execute(
                    text("SELECT count(*) FROM scenarios WHERE source_draft_ref = :ref"),
                    {"ref": draft_id},
                )
            ).scalar_one()
            assert count == 1
    finally:
        async with engine.connect() as conn, conn.begin():
            await conn.execute(
                text("DELETE FROM discovery_drafts WHERE run_id = :run_id"), {"run_id": run_id}
            )
        await _cleanup(engine, agent_id=agent_id)


async def test_add_scenario_suite_not_found() -> None:
    async with await _client() as client:
        response = await client.post(
            f"/v1/suites/{uuid.uuid4()}/scenarios",
            json={"name": "X", "persona": "Y"},
        )
    assert response.status_code == 404


async def test_update_and_delete_scenario() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine, "Scenario CRUD Agent")
    suite_id = await _make_suite(engine, agent_id, "Scenario CRUD Suite")
    scenario_id = await _make_scenario(engine, suite_id, "Original Scenario Name")

    try:
        async with await _client() as client:
            update_response = await client.patch(
                f"/v1/scenarios/{scenario_id}", json={"name": "Updated Scenario Name"}
            )
            assert update_response.status_code == 200
            assert update_response.json()["name"] == "Updated Scenario Name"

            delete_response = await client.delete(f"/v1/scenarios/{scenario_id}")
            assert delete_response.status_code == 204

            second_delete = await client.delete(f"/v1/scenarios/{scenario_id}")
            assert second_delete.status_code == 404
    finally:
        await _cleanup(engine, agent_id=agent_id)
