"""B1-04 tests: POST /v1/simulations|discovery|redteam/runs.

Idempotency-Key double-submit, concurrency pre-check, and the
discovery-specific invalid_identity 422 path are the ticket's named
acceptance criteria.
"""

import uuid

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import get_settings
from app.main import app
from tests.conftest import _test_engine, requires_test_db

pytestmark = requires_test_db

_HEADERS = {
    "Authorization": f"Bearer {get_settings().python_service_token}",
    "X-User-Id": "user-1",
}

_DUMMY_IDENTITY = {
    "name": "Priya Sharma",
    "dob": "1990-03-14",
    "account": "12345",
    "verificationPhrase": "blue umbrella",
}


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_HEADERS)


async def _make_agent(engine: AsyncEngine, *, max_concurrency: int = 1) -> uuid.UUID:
    agent_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO agents (id, name, transport, max_concurrency, created_by_user_id) "
                "VALUES (:id, 'Run Creation Agent', 'web', :max_concurrency, 'user-1')"
            ),
            {"id": agent_id, "max_concurrency": max_concurrency},
        )
    return agent_id


async def _make_suite_and_scenario(engine: AsyncEngine, agent_id: uuid.UUID) -> uuid.UUID:
    suite_id = uuid.uuid4()
    scenario_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO suites (id, name, agent_id, created_by_user_id) "
                "VALUES (:id, 'Run Creation Suite', :agent_id, 'user-1')"
            ),
            {"id": suite_id, "agent_id": agent_id},
        )
        await conn.execute(
            text(
                "INSERT INTO scenarios (id, suite_id, name, persona, persona_initials, source) "
                "VALUES (:id, :suite_id, 'Run Creation Scenario', 'Priya', 'PR', 'manual')"
            ),
            {"id": scenario_id, "suite_id": suite_id},
        )
    return scenario_id


async def _cleanup(engine: AsyncEngine, agent_id: uuid.UUID) -> None:
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "DELETE FROM run_events WHERE run_id IN (SELECT id FROM runs WHERE agent_id = :id)"
            ),
            {"id": agent_id},
        )
        await conn.execute(text("DELETE FROM runs WHERE agent_id = :id"), {"id": agent_id})
        await conn.execute(text("DELETE FROM suites WHERE agent_id = :id"), {"id": agent_id})
        await conn.execute(text("DELETE FROM agents WHERE id = :id"), {"id": agent_id})


async def test_create_simulation_run_derives_agent_from_scenario() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine, max_concurrency=5)
    scenario_id = await _make_suite_and_scenario(engine, agent_id)
    try:
        async with await _client() as client:
            response = await client.post(
                "/v1/simulations/runs", json={"scenarioId": str(scenario_id)}
            )
        assert response.status_code == 202
        run_id = response.json()["runId"]

        async with engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT agent_id, type, status FROM runs WHERE id = :id"),
                        {"id": uuid.UUID(run_id)},
                    )
                )
                .mappings()
                .one()
            )
        assert row["agent_id"] == agent_id
        assert row["type"] == "simulation"
        assert row["status"] == "queued"
    finally:
        await _cleanup(engine, agent_id)


async def test_create_simulation_run_unknown_scenario_is_404() -> None:
    async with await _client() as client:
        response = await client.post("/v1/simulations/runs", json={"scenarioId": str(uuid.uuid4())})
    assert response.status_code == 404


async def test_idempotency_key_double_submit_returns_same_run_id() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine, max_concurrency=5)
    scenario_id = await _make_suite_and_scenario(engine, agent_id)
    key = f"idem-{uuid.uuid4()}"
    try:
        async with await _client() as client:
            first = await client.post(
                "/v1/simulations/runs",
                json={"scenarioId": str(scenario_id)},
                headers={"Idempotency-Key": key},
            )
            second = await client.post(
                "/v1/simulations/runs",
                json={"scenarioId": str(scenario_id)},
                headers={"Idempotency-Key": key},
            )
        assert first.status_code == 202
        assert second.status_code == 202
        assert first.json()["runId"] == second.json()["runId"]

        async with engine.connect() as conn:
            count = (
                await conn.execute(
                    text("SELECT count(*) FROM runs WHERE idempotency_key = :key"), {"key": key}
                )
            ).scalar_one()
        assert count == 1
    finally:
        await _cleanup(engine, agent_id)


async def test_concurrency_limit_returns_409_with_retry_after() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine, max_concurrency=1)
    scenario_id = await _make_suite_and_scenario(engine, agent_id)
    try:
        async with await _client() as client:
            first = await client.post("/v1/simulations/runs", json={"scenarioId": str(scenario_id)})
            assert first.status_code == 202

            second = await client.post(
                "/v1/simulations/runs", json={"scenarioId": str(scenario_id)}
            )
        assert second.status_code == 409
        body = second.json()
        assert body["error"]["code"] == "concurrency_limit"
        assert body["error"]["details"]["retryAfterMs"] > 0
    finally:
        await _cleanup(engine, agent_id)


async def test_create_discovery_run_success() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine, max_concurrency=5)
    try:
        async with await _client() as client:
            response = await client.post(
                "/v1/discovery/runs",
                json={"agentId": str(agent_id), "dummyIdentity": _DUMMY_IDENTITY},
            )
        assert response.status_code == 202

        async with engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT type, config FROM runs WHERE id = :id"),
                        {"id": uuid.UUID(response.json()["runId"])},
                    )
                )
                .mappings()
                .one()
            )
        assert row["type"] == "discovery"
        assert row["config"]["dummyIdentity"]["name"] == "Priya Sharma"
    finally:
        await _cleanup(engine, agent_id)


async def test_create_discovery_run_invalid_identity_is_422() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine, max_concurrency=5)
    try:
        async with await _client() as client:
            response = await client.post(
                "/v1/discovery/runs",
                json={
                    "agentId": str(agent_id),
                    "dummyIdentity": {**_DUMMY_IDENTITY, "dob": "not-a-date"},
                },
            )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_identity"
        details = response.json()["error"]["details"]
        assert any("dob" in "".join(str(loc) for loc in err["loc"]) for err in details)
    finally:
        await _cleanup(engine, agent_id)


async def test_create_discovery_run_valid_format_wrong_identity_is_not_an_error() -> None:
    """spine §6: a valid-format-but-wrong identity is not a POST-time error --
    the run proceeds and gated branches come back blocked once discovery is
    actually implemented (B5). Only format failures 422 here."""
    engine = _test_engine()
    agent_id = await _make_agent(engine, max_concurrency=5)
    try:
        async with await _client() as client:
            response = await client.post(
                "/v1/discovery/runs",
                json={
                    "agentId": str(agent_id),
                    "dummyIdentity": {**_DUMMY_IDENTITY, "name": "Someone Else"},
                },
            )
        assert response.status_code == 202
    finally:
        await _cleanup(engine, agent_id)


async def test_create_redteam_run_success() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine, max_concurrency=5)
    try:
        async with await _client() as client:
            response = await client.post(
                "/v1/redteam/runs",
                json={"agentId": str(agent_id), "categories": ["PII", "Auth"]},
            )
        assert response.status_code == 202

        async with engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT type, config FROM runs WHERE id = :id"),
                        {"id": uuid.UUID(response.json()["runId"])},
                    )
                )
                .mappings()
                .one()
            )
        assert row["type"] == "redteam"
        assert row["config"]["categories"] == ["PII", "Auth"]
    finally:
        await _cleanup(engine, agent_id)


async def test_create_redteam_run_invalid_category_is_422() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine, max_concurrency=5)
    try:
        async with await _client() as client:
            response = await client.post(
                "/v1/redteam/runs",
                json={"agentId": str(agent_id), "categories": ["NotACategory"]},
            )
        assert response.status_code == 422
    finally:
        await _cleanup(engine, agent_id)


async def test_create_run_requires_user_id() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine, max_concurrency=5)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {get_settings().python_service_token}"},
        ) as client:
            response = await client.post(
                "/v1/redteam/runs",
                json={"agentId": str(agent_id), "categories": ["PII"]},
            )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "missing_user_id"
    finally:
        await _cleanup(engine, agent_id)


async def test_create_run_unknown_agent_is_404() -> None:
    async with await _client() as client:
        response = await client.post(
            "/v1/redteam/runs",
            json={"agentId": str(uuid.uuid4()), "categories": ["PII"]},
        )
    assert response.status_code == 404
