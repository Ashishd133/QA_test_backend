"""B2-06: GET /v1/projects -- returns the seeded default project only.
No UI, no switcher, no create/update endpoints yet (ticket: "schema and
plumbing only"); this exists so the frontend/API contract has somewhere to
read a projectId from ahead of B2-08 threading it through run creation.
"""

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import get_engine
from app.schemas.projects import Project

router = APIRouter(tags=["projects"])

_LIST_PROJECTS_SQL = text("SELECT id, name FROM projects ORDER BY name")


@router.get("/v1/projects", response_model=list[Project])
async def list_projects(engine: AsyncEngine = Depends(get_engine)) -> list[Project]:
    async with engine.connect() as conn:
        rows = (await conn.execute(_LIST_PROJECTS_SQL)).mappings().all()
    return [Project(id=str(row["id"]), name=row["name"]) for row in rows]
