"""B2.7-03 tests: /v1/test-profiles CRUD, encryption at rest, and access
logging.

`FIELD_ENCRYPTION_KEYS` is set by tests/conftest.py before any import of
app.crypto, so encrypt/decrypt round-trips for real here (no mocking
needed, unlike test_personas.py's TTS voice registry -- app.crypto never
calls a live external service).
"""

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


async def _cleanup(engine: AsyncEngine, profile_ids: list[uuid.UUID]) -> None:
    async with engine.connect() as conn, conn.begin():
        for profile_id in profile_ids:
            await conn.execute(
                text("DELETE FROM sensitive_access_log WHERE resource_id = :id"),
                {"id": profile_id},
            )
            await conn.execute(text("DELETE FROM test_profiles WHERE id = :id"), {"id": profile_id})


async def test_create_test_profile_round_trips_plaintext_but_stores_ciphertext() -> None:
    engine = _test_engine()
    async with await _client() as client:
        response = await client.post(
            "/v1/test-profiles",
            json={"name": "Dummy Borrower", "fields": {"ssn": "123-45-6789", "dob": "1990-01-01"}},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "Dummy Borrower"
        assert body["fields"] == {"ssn": "123-45-6789", "dob": "1990-01-01"}
        assert body["encrypted"] is True

        async with engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT fields FROM test_profiles WHERE id = :id"),
                        {"id": uuid.UUID(body["id"])},
                    )
                )
                .mappings()
                .one()
            )
        assert set(row["fields"]) == {"v", "ct"}
        assert "123-45-6789" not in row["fields"]["ct"]
    try:
        await _cleanup(engine, [uuid.UUID(body["id"])])
    finally:
        await engine.dispose()


async def test_create_test_profile_missing_user_id_is_400() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=auth_headers(user_id=None),
    ) as client:
        response = await client.post(
            "/v1/test-profiles", json={"name": "No User Profile", "fields": {"a": "b"}}
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "missing_user_id"


async def test_list_test_profiles_omits_fields() -> None:
    engine = _test_engine()
    async with await _client() as client:
        create_response = await client.post(
            "/v1/test-profiles", json={"name": "Listed Profile", "fields": {"a": "b"}}
        )
        profile_id = create_response.json()["id"]

        list_response = await client.get("/v1/test-profiles")
        assert list_response.status_code == 200
        items = list_response.json()
        matching = next(p for p in items if p["id"] == profile_id)
        assert "fields" not in matching
        assert matching["name"] == "Listed Profile"
    try:
        await _cleanup(engine, [uuid.UUID(profile_id)])
    finally:
        await engine.dispose()


async def test_get_test_profile_decrypts_and_access_logs() -> None:
    engine = _test_engine()
    async with await _client() as client:
        create_response = await client.post(
            "/v1/test-profiles", json={"name": "Detail Profile", "fields": {"acct": "999"}}
        )
        profile_id = create_response.json()["id"]

        get_response = await client.get(f"/v1/test-profiles/{profile_id}")
        assert get_response.status_code == 200
        assert get_response.json()["fields"] == {"acct": "999"}

        missing_response = await client.get(f"/v1/test-profiles/{uuid.uuid4()}")
        assert missing_response.status_code == 404

        async with engine.connect() as conn:
            log_rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT action FROM sensitive_access_log "
                            "WHERE resource_id = :id AND resource_type = 'test_profile'"
                        ),
                        {"id": uuid.UUID(profile_id)},
                    )
                )
                .mappings()
                .all()
            )
        assert [r["action"] for r in log_rows] == ["read"]
    try:
        await _cleanup(engine, [uuid.UUID(profile_id)])
    finally:
        await engine.dispose()


async def test_list_test_profiles_does_not_access_log() -> None:
    engine = _test_engine()
    async with await _client() as client:
        create_response = await client.post(
            "/v1/test-profiles", json={"name": "Unlogged Profile", "fields": {"a": "b"}}
        )
        profile_id = create_response.json()["id"]

        await client.get("/v1/test-profiles")

        async with engine.connect() as conn:
            log_rows = (
                (
                    await conn.execute(
                        text("SELECT action FROM sensitive_access_log WHERE resource_id = :id"),
                        {"id": uuid.UUID(profile_id)},
                    )
                )
                .mappings()
                .all()
            )
        assert log_rows == []
    try:
        await _cleanup(engine, [uuid.UUID(profile_id)])
    finally:
        await engine.dispose()


async def test_update_test_profile_partial_patch() -> None:
    engine = _test_engine()
    async with await _client() as client:
        create_response = await client.post(
            "/v1/test-profiles", json={"name": "Patchable Profile", "fields": {"a": "b"}}
        )
        profile_id = create_response.json()["id"]

        rename_response = await client.patch(
            f"/v1/test-profiles/{profile_id}", json={"name": "Renamed Profile"}
        )
        assert rename_response.status_code == 200
        assert rename_response.json()["name"] == "Renamed Profile"
        # Fields untouched by this PATCH still come back decrypted.
        assert rename_response.json()["fields"] == {"a": "b"}

        refield_response = await client.patch(
            f"/v1/test-profiles/{profile_id}", json={"fields": {"c": "d"}}
        )
        assert refield_response.status_code == 200
        assert refield_response.json()["fields"] == {"c": "d"}
        assert refield_response.json()["name"] == "Renamed Profile"
    try:
        await _cleanup(engine, [uuid.UUID(profile_id)])
    finally:
        await engine.dispose()


async def test_delete_test_profile_has_no_in_use_guard() -> None:
    engine = _test_engine()
    async with await _client() as client:
        create_response = await client.post(
            "/v1/test-profiles", json={"name": "Deletable Profile", "fields": {"a": "b"}}
        )
        profile_id = create_response.json()["id"]

        delete_response = await client.delete(f"/v1/test-profiles/{profile_id}")
        assert delete_response.status_code == 204

        missing_response = await client.get(f"/v1/test-profiles/{profile_id}")
        assert missing_response.status_code == 404
    await _cleanup(engine, [])
    await engine.dispose()


async def test_get_test_profile_in_another_project_is_404() -> None:
    """The default client's own project is real and visible (so this
    exercises `ensure_project_match`'s cross-tenant 404, not
    `require_project_id`'s "caller can't even see this project" 404 --
    those are different checks, see app/deps.py)."""
    engine = _test_engine()
    other_project_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text("INSERT INTO projects (id, name) VALUES (:id, 'Other Test Profile Project')"),
            {"id": other_project_id},
        )
        profile_id = uuid.uuid4()
        await conn.execute(
            text(
                "INSERT INTO test_profiles "
                "(id, project_id, name, fields, encrypted, created_by_user_id) "
                "VALUES (:id, :project_id, 'Other Project Profile', "
                "CAST(:fields AS jsonb), true, 'user-1')"
            ),
            {"id": profile_id, "project_id": other_project_id, "fields": '{"v": 1, "ct": "x"}'},
        )

    async with await _client() as client:
        response = await client.get(f"/v1/test-profiles/{profile_id}")
    assert response.status_code == 404
    async with engine.connect() as conn, conn.begin():
        await conn.execute(text("DELETE FROM test_profiles WHERE id = :id"), {"id": profile_id})
        await conn.execute(text("DELETE FROM projects WHERE id = :id"), {"id": other_project_id})
    await engine.dispose()


async def test_resolve_dummy_identity_prefers_test_profile_id_over_legacy() -> None:
    from app.api.test_profiles import resolve_dummy_identity

    engine = _test_engine()
    async with await _client() as client:
        create_response = await client.post(
            "/v1/test-profiles", json={"name": "Discovery Profile", "fields": {"ssn": "111"}}
        )
        profile_id = create_response.json()["id"]

    resolved = await resolve_dummy_identity(engine, {"testProfileId": profile_id}, user_id="user-1")
    assert resolved == {"ssn": "111"}

    both_present_resolved = await resolve_dummy_identity(
        engine,
        {"testProfileId": profile_id, "dummyIdentity": {"ssn": "999"}},
        user_id="user-1",
    )
    assert both_present_resolved == {"ssn": "111"}

    legacy_resolved = await resolve_dummy_identity(
        engine, {"dummyIdentity": {"ssn": "222"}}, user_id="user-1"
    )
    assert legacy_resolved == {"ssn": "222"}

    none_resolved = await resolve_dummy_identity(engine, {}, user_id="user-1")
    assert none_resolved is None

    async with engine.connect() as conn:
        log_rows = (
            (
                await conn.execute(
                    text(
                        "SELECT action FROM sensitive_access_log "
                        "WHERE resource_id = :id AND action = 'discovery_resolve'"
                    ),
                    {"id": uuid.UUID(profile_id)},
                )
            )
            .mappings()
            .all()
        )
    assert len(log_rows) == 2
    try:
        await _cleanup(engine, [uuid.UUID(profile_id)])
    finally:
        await engine.dispose()
