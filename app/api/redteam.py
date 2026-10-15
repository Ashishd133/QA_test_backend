"""B4-04: POST /v1/redteam/runs -- a red-team run is a *batch*.

One parent (type='redteam') plus one child Call per attack pack in the
selected categories, so the `/runs` queue, the call drawer and the History
table show a red-team run exactly as they show a suite batch ("no special
casing"). Each child records the pack id + version it will run, which is
the audit trail for what was attempted. The response is the parent's id as
`runId`, keeping the pre-B4 response shape.

Fan-out mirrors `POST /v1/suites/{id}/run`: same batch cap (`batch_too_large`),
same idempotency-key behavior, and the same rule that concurrency is
enforced at claim time (B3-03), not at creation.
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, Header, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.runs import _validate_dummy_identity
from app.api.test_profiles import store_dummy_identity
from app.config import get_settings
from app.db import get_engine
from app.deps import require_project_id, require_user_id
from app.engine.redteam.packs import select_packs
from app.errors import APIError
from app.schemas.errors import BATCH_TOO_LARGE_RESPONSE
from app.schemas.runs import RedteamRunCreate, RunCreateResponse

router = APIRouter(tags=["redteam"])

_AGENT_VISIBLE_SQL = text("SELECT 1 FROM agents WHERE id = :id AND project_id = :project_id")

_INSERT_PARENT_SQL = text(
    "INSERT INTO runs (id, project_id, type, agent_id, config, idempotency_key, "
    " created_by_user_id, trigger) "
    "VALUES (:id, :project_id, 'redteam', :agent_id, CAST(:config AS jsonb), :idempotency_key, "
    " :user_id, 'manual') "
    "ON CONFLICT (project_id, idempotency_key) DO NOTHING "
    "RETURNING id"
)
_EXISTING_BY_KEY_SQL = text(
    "SELECT id FROM runs WHERE project_id = :project_id AND idempotency_key = :key"
)
# Children are real single-attack calls: type='redteam' dispatches to the
# red-team executor, parent_run_id makes them Calls under the parent and
# excludes the parent itself from claiming (B3-03: only childless rows are
# claimable). Trigger is inherited from the parent.
_INSERT_CHILD_SQL = text(
    "INSERT INTO runs (id, project_id, type, agent_id, parent_run_id, config, "
    " created_by_user_id, trigger) "
    "VALUES (:id, :project_id, 'redteam', :agent_id, :parent_run_id, CAST(:config AS jsonb), "
    " :user_id, 'manual')"
)


@router.post(
    "/v1/redteam/runs",
    response_model=RunCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=BATCH_TOO_LARGE_RESPONSE,
)
async def create_redteam_run(
    body: RedteamRunCreate,
    user_id: str = Depends(require_user_id),
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> RunCreateResponse:
    agent_id = uuid.UUID(body.agent_id)
    packs = select_packs(body.categories)
    if not packs:
        raise APIError(
            "validation_error",
            "no attack packs exist for the selected categories",
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    if body.dummy_identity is not None:
        _validate_dummy_identity(body.dummy_identity)
    else:
        needs_identity = [p.id for p in packs if p.requires_verified]
        if needs_identity:
            raise APIError(
                "identity_required",
                "these attacks run from a verified session and need a dummyIdentity: "
                + ", ".join(needs_identity),
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                details={"packs": needs_identity},
            )

    cap = get_settings().suite_run_batch_cap
    if len(packs) > cap:
        raise APIError(
            "batch_too_large",
            f"this run would queue {len(packs)} calls, over the cap of {cap}",
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    async with engine.connect() as conn:
        visible = (
            await conn.execute(_AGENT_VISIBLE_SQL, {"id": agent_id, "project_id": project_id})
        ).first()
    if visible is None:
        # Same answer for "no such agent" and "another project's agent".
        raise APIError("not_found", "agent not found", status.HTTP_404_NOT_FOUND)

    profile_id: uuid.UUID | None = None
    if body.dummy_identity is not None:
        profile_id = await store_dummy_identity(
            engine,
            project_id=project_id,
            user_id=user_id,
            name="Red-team dummy identity",
            identity=body.dummy_identity.model_dump(mode="json", by_alias=True),
        )

    parent_id = uuid.uuid4()
    child_config_extra = {"testProfileId": str(profile_id)} if profile_id is not None else {}
    try:
        async with engine.connect() as conn, conn.begin():
            inserted = (
                (
                    await conn.execute(
                        _INSERT_PARENT_SQL,
                        {
                            "id": parent_id,
                            "project_id": project_id,
                            "agent_id": agent_id,
                            "config": json.dumps(
                                {"categories": body.categories, "packs": [p.id for p in packs]}
                            ),
                            "idempotency_key": idempotency_key,
                            "user_id": user_id,
                        },
                    )
                )
                .mappings()
                .first()
            )
            if inserted is None:
                # Same Idempotency-Key as an earlier request: that batch, not a second.
                existing = (
                    await conn.execute(
                        _EXISTING_BY_KEY_SQL, {"project_id": project_id, "key": idempotency_key}
                    )
                ).scalar_one()
                parent_id = uuid.UUID(str(existing))
                profile_id = None  # the unused profile is cleaned up below
            else:
                await conn.execute(
                    _INSERT_CHILD_SQL,
                    [
                        {
                            "id": uuid.uuid4(),
                            "project_id": project_id,
                            "agent_id": agent_id,
                            "parent_run_id": parent_id,
                            "config": json.dumps(
                                {
                                    "packId": p.id,
                                    "packVersion": p.version,
                                    "category": p.category,
                                    **child_config_extra,
                                }
                            ),
                            "user_id": user_id,
                        }
                        for p in packs
                    ],
                )
    except Exception:
        await _drop_profile(engine, child_config_extra)
        raise
    if inserted is None:
        await _drop_profile(engine, child_config_extra)
    return RunCreateResponse(run_id=str(parent_id))


async def _drop_profile(engine: AsyncEngine, config_extra: dict[str, str]) -> None:
    """The profile insert commits on its own, before the batch does; if the
    batch never came to exist (insert failed, or the Idempotency-Key matched
    an earlier batch), delete the now-orphaned identity rather than leave it
    behind with nothing pointing at it."""
    profile = config_extra.get("testProfileId")
    if profile is None:
        return
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text("DELETE FROM test_profiles WHERE id = :id"), {"id": uuid.UUID(profile)}
        )
