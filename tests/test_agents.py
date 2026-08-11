"""B1-02 tests: /v1/agents against the isolated test DB.

ConnectedAgent/AgentHealth shapes are pinned to the frontend's
src/types/index.ts. The per-transport config shapes (Web/Sip/PhoneConfig)
and the rest of this router's design are this repo's own -- no frontend
form exists yet -- see app/api/agents.py's module docstring.
"""

import uuid

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import get_settings
from app.main import app
from tests.conftest import _test_engine, auth_headers, requires_test_db

pytestmark = requires_test_db

_HEADERS = auth_headers()

_WEB_CONFIG = {"transport": "web", "roomUrl": "https://example.livekit.cloud/room", "token": "tok"}


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_HEADERS)


async def _cleanup(engine: AsyncEngine, agent_id: uuid.UUID) -> None:
    async with engine.connect() as conn, conn.begin():
        await conn.execute(text("DELETE FROM runs WHERE agent_id = :id"), {"id": agent_id})
        await conn.execute(text("DELETE FROM suites WHERE agent_id = :id"), {"id": agent_id})
        await conn.execute(text("DELETE FROM agents WHERE id = :id"), {"id": agent_id})


async def test_create_agent_web_transport_round_trips_config() -> None:
    async with await _client() as client:
        response = await client.post(
            "/v1/agents", json={"name": "Web Agent", "config": _WEB_CONFIG}
        )
    assert response.status_code == 201
    body = response.json()
    assert body["transport"] == "web"
    assert body["config"]["roomUrl"] == "https://example.livekit.cloud/room"
    assert body["maxConcurrency"] == 1

    engine = _test_engine()
    try:
        await _cleanup(engine, uuid.UUID(body["id"]))
    finally:
        await engine.dispose()


async def test_create_agent_missing_user_id_is_400() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {get_settings().python_service_token}"},
    ) as client:
        response = await client.post(
            "/v1/agents", json={"name": "No User Agent", "config": _WEB_CONFIG}
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "missing_user_id"


async def test_create_agent_invalid_transport_config_is_422_with_field_details() -> None:
    async with await _client() as client:
        response = await client.post(
            "/v1/agents",
            json={"name": "Bad Agent", "config": {"transport": "sip"}},
        )
    assert response.status_code == 422
    details = response.json()["error"]["details"]
    assert any("sipUri" in "".join(str(loc) for loc in err["loc"]) for err in details)


async def test_create_agent_unknown_transport_discriminator_is_422() -> None:
    async with await _client() as client:
        response = await client.post(
            "/v1/agents",
            json={"name": "Unknown Transport Agent", "config": {"transport": "carrier-pigeon"}},
        )
    assert response.status_code == 422


async def test_get_agent_detail_and_404() -> None:
    engine = _test_engine()
    async with await _client() as client:
        create_response = await client.post(
            "/v1/agents", json={"name": "Detail Agent", "config": _WEB_CONFIG}
        )
        agent_id = create_response.json()["id"]

        get_response = await client.get(f"/v1/agents/{agent_id}")
        assert get_response.status_code == 200
        assert get_response.json()["name"] == "Detail Agent"

        missing_response = await client.get(f"/v1/agents/{uuid.uuid4()}")
        assert missing_response.status_code == 404
    try:
        await _cleanup(engine, uuid.UUID(agent_id))
    finally:
        await engine.dispose()


async def test_list_agents_and_health_shapes() -> None:
    engine = _test_engine()
    async with await _client() as client:
        create_response = await client.post(
            "/v1/agents",
            json={"name": "List Agent", "config": _WEB_CONFIG, "language": "en"},
        )
        agent_id = create_response.json()["id"]

        list_response = await client.get("/v1/agents")
        assert list_response.status_code == 200
        item = next(a for a in list_response.json() if a["id"] == agent_id)
        assert item["tag"] == "RTC"
        assert item["status"] == "Offline"
        assert item["meta"] == "Web · en"

        health_response = await client.get("/v1/agents/health")
        assert health_response.status_code == 200
        health_item = next(h for h in health_response.json() if h["id"] == agent_id)
        assert health_item["uptime"] == "Never probed"
        assert health_item["dotColor"] == "red"
    try:
        await _cleanup(engine, uuid.UUID(agent_id))
    finally:
        await engine.dispose()


async def test_update_agent_partial_patch_and_transport_swap() -> None:
    engine = _test_engine()
    async with await _client() as client:
        create_response = await client.post(
            "/v1/agents", json={"name": "Patchable Agent", "config": _WEB_CONFIG}
        )
        agent_id = create_response.json()["id"]

        patch_response = await client.patch(
            f"/v1/agents/{agent_id}", json={"name": "Renamed Agent"}
        )
        assert patch_response.status_code == 200
        assert patch_response.json()["name"] == "Renamed Agent"
        assert patch_response.json()["transport"] == "web"

        swap_response = await client.patch(
            f"/v1/agents/{agent_id}",
            json={"config": {"transport": "phone", "phoneNumber": "+15551234567"}},
        )
        assert swap_response.status_code == 200
        assert swap_response.json()["transport"] == "phone"
        assert swap_response.json()["config"]["phoneNumber"] == "+15551234567"

        missing_response = await client.patch(f"/v1/agents/{uuid.uuid4()}", json={"name": "X"})
        assert missing_response.status_code == 404
    try:
        await _cleanup(engine, uuid.UUID(agent_id))
    finally:
        await engine.dispose()


async def test_delete_agent_and_conflict_when_in_use() -> None:
    engine = _test_engine()
    async with await _client() as client:
        create_response = await client.post(
            "/v1/agents", json={"name": "Deletable Agent", "config": _WEB_CONFIG}
        )
        agent_id = create_response.json()["id"]

        suite_response = await client.post(
            "/v1/suites", json={"name": "Blocking Suite", "agentId": agent_id}
        )
        assert suite_response.status_code == 201
        suite_id = suite_response.json()["id"]

        conflict_response = await client.delete(f"/v1/agents/{agent_id}")
        assert conflict_response.status_code == 409
        assert conflict_response.json()["error"]["code"] == "conflict"

        await client.delete(f"/v1/suites/{suite_id}")

        delete_response = await client.delete(f"/v1/agents/{agent_id}")
        assert delete_response.status_code == 204

        second_delete = await client.delete(f"/v1/agents/{agent_id}")
        assert second_delete.status_code == 404
    try:
        await _cleanup(engine, uuid.UUID(agent_id))
    finally:
        await engine.dispose()


async def test_test_connection_pre_save_web_succeeds_sip_phone_not_supported() -> None:
    async with await _client() as client:
        web_response = await client.post("/v1/agents/test-connection", json=_WEB_CONFIG)
        assert web_response.status_code == 200
        assert web_response.json()["success"] is True

        sip_response = await client.post(
            "/v1/agents/test-connection",
            json={"transport": "sip", "sipUri": "sip:agent@example.com"},
        )
        assert sip_response.status_code == 501
        assert sip_response.json()["error"]["code"] == "not_supported"


async def test_test_connection_post_save_uses_saved_transport() -> None:
    engine = _test_engine()
    async with await _client() as client:
        create_response = await client.post(
            "/v1/agents",
            json={"name": "SIP Agent", "config": {"transport": "sip", "sipUri": "sip:a@b.com"}},
        )
        agent_id = create_response.json()["id"]

        response = await client.post(f"/v1/agents/{agent_id}/test-connection")
        assert response.status_code == 501

        missing_response = await client.post(f"/v1/agents/{uuid.uuid4()}/test-connection")
        assert missing_response.status_code == 404
    try:
        await _cleanup(engine, uuid.UUID(agent_id))
    finally:
        await engine.dispose()
