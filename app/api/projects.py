"""B2.5-01/02: /v1/projects CRUD + hard scoping.

Supersedes B2-06's read-only stub (`GET /v1/projects` returning the seeded
default project only, no create/update). `agents`/`suites`/`runs.project_id`
are NOT NULL as of migration 005 -- every write to those tables now stamps
`project_id` from `X-Project-Id` (app.deps.require_project_id), and every
list/detail read filters or checks against it. Cross-project access 404s,
never 403 (spine rule: don't leak the existence of another project's
resources).

`list_projects` returns every project in orgs the caller is a member of
(migration 006's `org_memberships`, backfilled from every distinct
`created_by_user_id` seen so far). Every project created here lands in the
default org (no `POST /v1/organizations` yet -- see app/api/organizations.py)
and its creator is added as an 'admin' member if not one already, so a
brand-new user id can create a project and immediately see it.

PATCH/DELETE take the project id from the path, not X-Project-Id, so they
sit outside app.deps.require_project_id's header-driven check (see its
exemption-list comment) -- but they are NOT unscoped: both require the
caller to be a member of the project's org, checked inline here.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.db import get_engine
from app.deps import require_user_id
from app.errors import APIError
from app.models.organizations import DEFAULT_ORG_ID
from app.schemas.projects import Project, ProjectCreate, ProjectUpdate

router = APIRouter(tags=["projects"])

_LIST_PROJECTS_SQL = text(
    "SELECT DISTINCT p.id, p.name, p.created_at FROM projects p "
    "JOIN org_memberships m ON m.org_id = p.org_id AND m.user_id = :user_id "
    "WHERE p.deleted_at IS NULL ORDER BY p.name"
)

_PROJECT_VISIBLE_TO_CALLER_SQL = text(
    "SELECT 1 FROM projects p "
    "JOIN org_memberships m ON m.org_id = p.org_id AND m.user_id = :user_id "
    "WHERE p.id = :id AND p.deleted_at IS NULL"
)


def _project(row: Any) -> Project:
    return Project(id=str(row["id"]), name=row["name"], created_at=row["created_at"].isoformat())


async def _ensure_project_visible(
    conn: AsyncConnection, project_id: uuid.UUID, user_id: str
) -> None:
    row = (
        await conn.execute(_PROJECT_VISIBLE_TO_CALLER_SQL, {"id": project_id, "user_id": user_id})
    ).first()
    if row is None:
        raise APIError("not_found", "project not found", status.HTTP_404_NOT_FOUND)


@router.get("/v1/projects", response_model=list[Project])
async def list_projects(
    user_id: str = Depends(require_user_id), engine: AsyncEngine = Depends(get_engine)
) -> list[Project]:
    async with engine.connect() as conn:
        rows = (await conn.execute(_LIST_PROJECTS_SQL, {"user_id": user_id})).mappings().all()
    return [_project(row) for row in rows]


@router.post("/v1/projects", response_model=Project, status_code=status.HTTP_201_CREATED)
async def create_project(
    body: ProjectCreate,
    user_id: str = Depends(require_user_id),
    engine: AsyncEngine = Depends(get_engine),
) -> Project:
    async with engine.connect() as conn, conn.begin():
        row = (
            (
                await conn.execute(
                    text(
                        "INSERT INTO projects (id, org_id, name) "
                        "VALUES (:id, :org_id, :name) "
                        "RETURNING id, name, created_at"
                    ),
                    {"id": uuid.uuid4(), "org_id": DEFAULT_ORG_ID, "name": body.name},
                )
            )
            .mappings()
            .one()
        )
        await conn.execute(
            text(
                "INSERT INTO org_memberships (org_id, user_id, role) "
                "VALUES (:org_id, :user_id, 'admin') ON CONFLICT (org_id, user_id) DO NOTHING"
            ),
            {"org_id": DEFAULT_ORG_ID, "user_id": user_id},
        )
    return _project(row)


@router.patch("/v1/projects/{project_id}", response_model=Project)
async def update_project(
    project_id: uuid.UUID,
    body: ProjectUpdate,
    user_id: str = Depends(require_user_id),
    engine: AsyncEngine = Depends(get_engine),
) -> Project:
    async with engine.connect() as conn, conn.begin():
        await _ensure_project_visible(conn, project_id, user_id)
        if body.name is not None:
            await conn.execute(
                text("UPDATE projects SET name = :name WHERE id = :id"),
                {"name": body.name, "id": project_id},
            )
        row = (
            (
                await conn.execute(
                    text("SELECT id, name, created_at FROM projects WHERE id = :id"),
                    {"id": project_id},
                )
            )
            .mappings()
            .one()
        )
    return _project(row)


_NON_EMPTY_SQL = text(
    "SELECT "
    "(SELECT count(*) FROM agents WHERE project_id = :id) "
    "+ (SELECT count(*) FROM suites WHERE project_id = :id) "
    "+ (SELECT count(*) FROM runs WHERE project_id = :id) AS n"
)


@router.delete("/v1/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: uuid.UUID,
    user_id: str = Depends(require_user_id),
    engine: AsyncEngine = Depends(get_engine),
) -> None:
    async with engine.connect() as conn, conn.begin():
        await _ensure_project_visible(conn, project_id, user_id)
        in_use = (await conn.execute(_NON_EMPTY_SQL, {"id": project_id})).scalar_one()
        if in_use:
            raise APIError(
                "conflict",
                "project has associated agents, suites or runs and cannot be deleted",
                status.HTTP_409_CONFLICT,
            )
        await conn.execute(
            text("UPDATE projects SET deleted_at = now() WHERE id = :id"), {"id": project_id}
        )
