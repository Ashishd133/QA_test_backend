"""B2.7-03: /v1/test-profiles CRUD + encryption at rest.

`fields` is encrypted app-layer (app.crypto's MultiFernet, shared with
B4-05's `findings.evidence` later) before it ever reaches the database --
the jsonb column holds `{"v": 1, "ct": "<token>"}`, never plaintext.
Decrypting a single profile writes a `sensitive_access_log` row in the
same transaction as the read; listing profiles does not decrypt or log at
all (see app.schemas.test_profiles.TestProfileSummary's docstring for why
that split exists).

Discovery's `dummyIdentity` migrates to reference a profile via this
module's `resolve_dummy_identity` -- see app/api/runs.py's
create_discovery_run, which now creates a profile instead of embedding
the identity inline in `runs.config`.
"""

import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, status
from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.crypto import decrypt_json, encrypt_json
from app.db import get_engine
from app.deps import ensure_project_match, require_project_id, require_user_id
from app.errors import APIError
from app.schemas.test_profiles import (
    TestProfileCreate,
    TestProfileDetail,
    TestProfileSummary,
    TestProfileUpdate,
)

router = APIRouter(tags=["test-profiles"])

_SUMMARY_COLUMNS = "id, project_id, name, encrypted"
_DETAIL_COLUMNS = "id, project_id, name, fields, encrypted"


def _test_profile_summary(row: RowMapping) -> TestProfileSummary:
    return TestProfileSummary(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        name=row["name"],
        encrypted=row["encrypted"],
    )


def _test_profile_detail(row: RowMapping, decrypted_fields: dict[str, object]) -> TestProfileDetail:
    return TestProfileDetail(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        name=row["name"],
        fields=decrypted_fields,
        encrypted=row["encrypted"],
    )


@router.get("/v1/test-profiles", response_model=list[TestProfileSummary])
async def list_test_profiles(
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
) -> list[TestProfileSummary]:
    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        f"SELECT {_SUMMARY_COLUMNS} FROM test_profiles "
                        "WHERE project_id = :project_id ORDER BY created_at"
                    ),
                    {"project_id": project_id},
                )
            )
            .mappings()
            .all()
        )
    return [_test_profile_summary(row) for row in rows]


@router.post(
    "/v1/test-profiles", response_model=TestProfileDetail, status_code=status.HTTP_201_CREATED
)
async def create_test_profile(
    body: TestProfileCreate,
    user_id: str = Depends(require_user_id),
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
) -> TestProfileDetail:
    envelope = encrypt_json(body.fields)
    async with engine.connect() as conn, conn.begin():
        row = (
            (
                await conn.execute(
                    text(
                        "INSERT INTO test_profiles "
                        "(id, project_id, name, fields, encrypted, created_by_user_id) "
                        "VALUES (:id, :project_id, :name, CAST(:fields AS jsonb), true, :user_id) "
                        f"RETURNING {_DETAIL_COLUMNS}"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "project_id": project_id,
                        "name": body.name,
                        "fields": _dump_json(envelope),
                        "user_id": user_id,
                    },
                )
            )
            .mappings()
            .one()
        )
    # The caller just supplied this plaintext themselves -- returning it
    # back is not a new disclosure, so this isn't access-logged the way a
    # read of already-stored ciphertext is.
    return _test_profile_detail(row, body.fields)


def _dump_json(value: object) -> str:
    return json.dumps(value)


async def _fetch_test_profile_or_404(
    conn: AsyncConnection, profile_id: uuid.UUID, project_id: uuid.UUID
) -> RowMapping:
    row = (
        (
            await conn.execute(
                text(f"SELECT {_DETAIL_COLUMNS} FROM test_profiles WHERE id = :id"),
                {"id": profile_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise APIError("not_found", "test profile not found", status.HTTP_404_NOT_FOUND)
    ensure_project_match(row["project_id"], project_id)
    return row


@router.get("/v1/test-profiles/{profile_id}", response_model=TestProfileDetail)
async def get_test_profile(
    profile_id: uuid.UUID,
    user_id: str = Depends(require_user_id),
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
) -> TestProfileDetail:
    async with engine.connect() as conn, conn.begin():
        row = await _fetch_test_profile_or_404(conn, profile_id, project_id)
        decrypted = decrypt_json(row["fields"])
        await conn.execute(
            text(
                "INSERT INTO sensitive_access_log "
                "(id, resource_type, resource_id, user_id, action) "
                "VALUES (:id, 'test_profile', :resource_id, :user_id, 'read')"
            ),
            {"id": uuid.uuid4(), "resource_id": profile_id, "user_id": user_id},
        )
    return _test_profile_detail(row, decrypted)


@router.patch("/v1/test-profiles/{profile_id}", response_model=TestProfileDetail)
async def update_test_profile(
    profile_id: uuid.UUID,
    body: TestProfileUpdate,
    user_id: str = Depends(require_user_id),
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
) -> TestProfileDetail:
    async with engine.connect() as conn, conn.begin():
        await _fetch_test_profile_or_404(conn, profile_id, project_id)

        updates: dict[str, Any] = {}
        if body.name is not None:
            updates["name"] = body.name
        if body.fields is not None:
            updates["fields"] = _dump_json(encrypt_json(body.fields))

        if updates:
            assignments = []
            params: dict[str, Any] = {"id": profile_id}
            for col, val in updates.items():
                clause = f"{col} = CAST(:{col} AS jsonb)" if col == "fields" else f"{col} = :{col}"
                assignments.append(clause)
                params[col] = val
            await conn.execute(
                text(f"UPDATE test_profiles SET {', '.join(assignments)} WHERE id = :id"), params
            )
        row = (
            (
                await conn.execute(
                    text(f"SELECT {_DETAIL_COLUMNS} FROM test_profiles WHERE id = :id"),
                    {"id": profile_id},
                )
            )
            .mappings()
            .one()
        )
    # Same reasoning as create: returning fields the caller just wrote
    # isn't a new disclosure event worth logging as a "read" the way GET
    # is. If they didn't touch `fields` this PATCH, decrypt the (unchanged)
    # stored value to show current state rather than omitting it.
    decrypted = body.fields if body.fields is not None else decrypt_json(row["fields"])
    return _test_profile_detail(row, decrypted)


@router.delete("/v1/test-profiles/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_test_profile(
    profile_id: uuid.UUID,
    user_id: str = Depends(require_user_id),
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
) -> None:
    # No in-use guard, unlike agents/personas: scenarios.test_profile_id is
    # ON DELETE SET NULL (app.models.suites), a deliberate, different
    # choice for this resource -- a deleted profile shouldn't take a
    # scenario down with it, just drop the reference (migration 010's own
    # reasoning).
    async with engine.connect() as conn, conn.begin():
        await _fetch_test_profile_or_404(conn, profile_id, project_id)
        await conn.execute(text("DELETE FROM test_profiles WHERE id = :id"), {"id": profile_id})


async def resolve_dummy_identity(
    engine: AsyncEngine, config: dict[str, object], *, user_id: str
) -> dict[str, object] | None:
    """Given a discovery run's `config`, returns the decrypted dummy
    identity dict -- checking `testProfileId` first (every run created
    after this ticket), falling back to the legacy inline `dummyIdentity`
    (any run created before it; there are none in this database as of
    B2.7-03, but the fallback costs nothing and this is exactly the
    ticket's own "an existing discovery run's inline identity still
    resolves after migration" bar). Returns None if neither is present --
    callers decide whether that's an error for them, not this function.
    """
    profile_id = config.get("testProfileId")
    if profile_id is not None:
        async with engine.connect() as conn, conn.begin():
            row = (
                (
                    await conn.execute(
                        text("SELECT fields FROM test_profiles WHERE id = :id"),
                        {"id": uuid.UUID(str(profile_id))},
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            decrypted = decrypt_json(row["fields"])
            await conn.execute(
                text(
                    "INSERT INTO sensitive_access_log "
                    "(id, resource_type, resource_id, user_id, action) "
                    "VALUES (:id, 'test_profile', :resource_id, :user_id, 'discovery_resolve')"
                ),
                {"id": uuid.uuid4(), "resource_id": uuid.UUID(str(profile_id)), "user_id": user_id},
            )
        return decrypted

    legacy = config.get("dummyIdentity")
    if isinstance(legacy, dict):
        return legacy
    return None
