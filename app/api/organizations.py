"""B2.5-02: GET /v1/organizations -- the caller's orgs, each with nested
projects and per-project agent counts, in one round trip (see this
module's docstring pair in app/schemas/organizations.py for why that
shape matters). Not project-scoped (app.deps's exemption list): this is
exactly the endpoint the switcher calls to learn which projects exist
before X-Project-Id can be set to any of them.

No POST /v1/organizations yet -- orgs are provisioned out-of-band
(migration 006 seeds the default org; a self-serve signup flow is E10).
"""

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import get_engine
from app.deps import require_user_id
from app.schemas.organizations import Organization, OrganizationProject

router = APIRouter(tags=["organizations"])

_ORGANIZATIONS_SQL = text(
    "SELECT o.id AS org_id, o.name AS org_name, o.slug AS org_slug, "
    "       p.id AS project_id, p.name AS project_name, "
    "       count(a.id) AS agent_count "
    "FROM organizations o "
    "JOIN org_memberships m ON m.org_id = o.id AND m.user_id = :user_id "
    "LEFT JOIN projects p ON p.org_id = o.id AND p.deleted_at IS NULL "
    "LEFT JOIN agents a ON a.project_id = p.id "
    "GROUP BY o.id, o.name, o.slug, p.id, p.name "
    "ORDER BY o.name, p.name"
)


@router.get("/v1/organizations", response_model=list[Organization])
async def list_organizations(
    user_id: str = Depends(require_user_id), engine: AsyncEngine = Depends(get_engine)
) -> list[Organization]:
    async with engine.connect() as conn:
        rows = (await conn.execute(_ORGANIZATIONS_SQL, {"user_id": user_id})).mappings().all()

    orgs: dict[str, Organization] = {}
    for row in rows:
        org_id = str(row["org_id"])
        org = orgs.setdefault(
            org_id,
            Organization(id=org_id, name=row["org_name"], slug=row["org_slug"], projects=[]),
        )
        if row["project_id"] is not None:
            org.projects.append(
                OrganizationProject(
                    id=str(row["project_id"]),
                    name=row["project_name"],
                    agent_count=row["agent_count"],
                )
            )
    return list(orgs.values())
