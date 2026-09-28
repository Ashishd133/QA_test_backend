"""B2.7-02 tests: /v1/personas CRUD, project scoping, and voice validation.

Voice/language validation normally hits the real Google Cloud TTS
ListVoices API (app.engine.caller.tts_voices, cached per-process) -- CI's
own workflow (ci.yml) carries no GCP credentials, exactly why judge_evals
got a separate workflow with its own. `_mock_voice_registry` patches the
module's cached lookup with a small fixed set instead, since
`is_supported`/`supported_languages`/`supported_voices` all resolve
`tts_voices._voice_registry` at call time (not import time), so
monkeypatching the module attribute is enough -- no live call, no
network dependency, for every test in this file.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.engine.caller import tts_voices
from app.main import app
from tests.conftest import _test_engine, auth_headers, requires_test_db

pytestmark = requires_test_db

_HEADERS = auth_headers()
_VALID_VOICE = {"language": "en-US", "voice": "en-US-Chirp3-HD-Charon"}
_MOCK_REGISTRY = {
    "en-US": frozenset({"en-US-Chirp3-HD-Charon", "en-US-Chirp3-HD-Puck"}),
    "en-IN": frozenset({"en-IN-Chirp3-HD-Achernar"}),
}


@pytest.fixture(autouse=True)
def _mock_voice_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tts_voices, "_voice_registry", lambda: _MOCK_REGISTRY)


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_HEADERS)


async def _cleanup(
    engine: AsyncEngine,
    persona_ids: list[uuid.UUID],
    agent_ids: list[uuid.UUID] | None = None,
) -> None:
    async with engine.connect() as conn, conn.begin():
        for persona_id in persona_ids:
            await conn.execute(
                text("UPDATE scenarios SET persona_id = NULL WHERE persona_id = :id"),
                {"id": persona_id},
            )
            await conn.execute(text("DELETE FROM personas WHERE id = :id"), {"id": persona_id})
        for agent_id in agent_ids or []:
            await conn.execute(text("DELETE FROM runs WHERE agent_id = :id"), {"id": agent_id})
            await conn.execute(text("DELETE FROM suites WHERE agent_id = :id"), {"id": agent_id})
            await conn.execute(text("DELETE FROM agents WHERE id = :id"), {"id": agent_id})


async def test_create_persona_round_trips_and_validates_voice() -> None:
    engine = _test_engine()
    async with await _client() as client:
        response = await client.post(
            "/v1/personas",
            json={"name": "Test Persona", "traits": {"tone": "calm"}, **_VALID_VOICE},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "Test Persona"
        assert body["builtin"] is False
        assert body["projectId"] is not None

        bad_response = await client.post(
            "/v1/personas",
            json={"name": "Bad Voice Persona", "language": "en-US", "voice": "not-a-real-voice"},
        )
        assert bad_response.status_code == 422
        error = bad_response.json()["error"]
        assert error["code"] == "unsupported_voice"
        assert "en-US-Chirp3-HD-Charon" in error["details"]["supportedVoicesForLanguage"]
    try:
        await _cleanup(engine, [uuid.UUID(body["id"])])
    finally:
        await engine.dispose()


async def test_create_persona_missing_user_id_is_400() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=auth_headers(user_id=None),
    ) as client:
        response = await client.post(
            "/v1/personas", json={"name": "No User Persona", **_VALID_VOICE}
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "missing_user_id"


async def test_list_personas_includes_builtins_and_project_scoped() -> None:
    engine = _test_engine()
    async with await _client() as client:
        create_response = await client.post(
            "/v1/personas", json={"name": "Scoped Persona", **_VALID_VOICE}
        )
        persona_id = create_response.json()["id"]

        list_response = await client.get("/v1/personas")
        assert list_response.status_code == 200
        items = list_response.json()
        assert any(p["id"] == persona_id for p in items)
        assert any(p["builtin"] is True and p["projectId"] is None for p in items)
    try:
        await _cleanup(engine, [uuid.UUID(persona_id)])
    finally:
        await engine.dispose()


async def test_get_persona_detail_and_404() -> None:
    engine = _test_engine()
    async with await _client() as client:
        create_response = await client.post(
            "/v1/personas", json={"name": "Detail Persona", **_VALID_VOICE}
        )
        persona_id = create_response.json()["id"]

        get_response = await client.get(f"/v1/personas/{persona_id}")
        assert get_response.status_code == 200
        assert get_response.json()["name"] == "Detail Persona"

        missing_response = await client.get(f"/v1/personas/{uuid.uuid4()}")
        assert missing_response.status_code == 404
    try:
        await _cleanup(engine, [uuid.UUID(persona_id)])
    finally:
        await engine.dispose()


async def test_update_persona_partial_patch_and_builtin_refused() -> None:
    engine = _test_engine()
    async with await _client() as client:
        create_response = await client.post(
            "/v1/personas", json={"name": "Patchable Persona", **_VALID_VOICE}
        )
        persona_id = create_response.json()["id"]

        patch_response = await client.patch(
            f"/v1/personas/{persona_id}", json={"name": "Renamed Persona"}
        )
        assert patch_response.status_code == 200
        assert patch_response.json()["name"] == "Renamed Persona"
        assert patch_response.json()["voice"] == _VALID_VOICE["voice"]

        bad_voice_response = await client.patch(
            f"/v1/personas/{persona_id}", json={"voice": "not-a-real-voice"}
        )
        assert bad_voice_response.status_code == 422

        builtins = (await client.get("/v1/personas")).json()
        builtin_id = next(p["id"] for p in builtins if p["builtin"] is True)
        builtin_patch = await client.patch(f"/v1/personas/{builtin_id}", json={"name": "Hacked"})
        assert builtin_patch.status_code == 409
        assert builtin_patch.json()["error"]["code"] == "builtin_read_only"
    try:
        await _cleanup(engine, [uuid.UUID(persona_id)])
    finally:
        await engine.dispose()


async def test_delete_persona_conflict_when_referenced_and_builtin_refused() -> None:
    engine = _test_engine()
    agent_id: str | None = None
    async with await _client() as client:
        create_response = await client.post(
            "/v1/personas", json={"name": "Deletable Persona", **_VALID_VOICE}
        )
        persona_id = create_response.json()["id"]

        agent_response = await client.post(
            "/v1/agents",
            json={
                "name": "Persona Test Agent",
                "config": {
                    "transport": "web",
                    "roomUrl": "https://example.livekit.cloud/room",
                    "token": "tok",
                },
            },
        )
        agent_id = agent_response.json()["id"]
        suite_response = await client.post(
            "/v1/suites", json={"name": "Persona Test Suite", "agentId": agent_id}
        )
        suite_id = suite_response.json()["id"]

        scenario_id = uuid.uuid4()
        async with engine.connect() as conn, conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO scenarios "
                    "(id, suite_id, name, source, persona_id) "
                    "VALUES (:id, :suite_id, 'Ref Scenario', 'manual', :persona_id)"
                ),
                {
                    "id": scenario_id,
                    "suite_id": uuid.UUID(suite_id),
                    "persona_id": uuid.UUID(persona_id),
                },
            )

        conflict_response = await client.delete(f"/v1/personas/{persona_id}")
        assert conflict_response.status_code == 409
        assert conflict_response.json()["error"]["code"] == "conflict"

        async with engine.connect() as conn, conn.begin():
            await conn.execute(text("DELETE FROM scenarios WHERE id = :id"), {"id": scenario_id})

        delete_response = await client.delete(f"/v1/personas/{persona_id}")
        assert delete_response.status_code == 204

        builtins = (await client.get("/v1/personas")).json()
        builtin_id = next(p["id"] for p in builtins if p["builtin"] is True)
        builtin_delete = await client.delete(f"/v1/personas/{builtin_id}")
        assert builtin_delete.status_code == 409
        assert builtin_delete.json()["error"]["code"] == "builtin_read_only"

        await client.delete(f"/v1/suites/{suite_id}")
    await _cleanup(engine, [], [uuid.UUID(agent_id)] if agent_id else [])
    await engine.dispose()


async def test_duplicate_persona_creates_editable_project_scoped_copy() -> None:
    engine = _test_engine()
    async with await _client() as client:
        builtins = (await client.get("/v1/personas")).json()
        builtin = next(p for p in builtins if p["builtin"] is True)

        dup_response = await client.post(f"/v1/personas/{builtin['id']}/duplicate")
        assert dup_response.status_code == 201
        dup = dup_response.json()
        assert dup["builtin"] is False
        assert dup["projectId"] is not None
        assert dup["voice"] == builtin["voice"]
        assert dup["name"] == f"{builtin['name']} (copy)"

        # The copy is editable even though its source wasn't.
        patch_response = await client.patch(
            f"/v1/personas/{dup['id']}", json={"name": "Edited Copy"}
        )
        assert patch_response.status_code == 200
    try:
        await _cleanup(engine, [uuid.UUID(dup["id"])])
    finally:
        await engine.dispose()
