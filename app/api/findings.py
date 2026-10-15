"""B4-05: the findings read path.

  GET   /v1/findings          project-wide triage list, evidence MASKED
  GET   /v1/findings/{id}     one finding in plaintext (access-logged)
  PATCH /v1/findings/{id}     resolve / reopen (feeds the dashboard's
                              "open critical findings" card)

The masked list is what a triage screen renders by default; "reveal" is a
deliberate read of one finding, which is the only way the leaked text leaves
the server besides a red-team call's own detail (also access-logged).
Findings are project-scoped through their run.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import get_engine
from app.deps import ensure_project_match, require_project_id, require_user_id
from app.engine.redteam.findings import FINDING_COLUMNS, finding_summary, reveal_findings
from app.errors import APIError
from app.schemas.findings import FindingDetail, FindingResolve, FindingSummary

router = APIRouter(tags=["findings"])

_LIST_SQL = text(
    f"SELECT {FINDING_COLUMNS} FROM findings f JOIN runs r ON r.id = f.run_id "
    "WHERE r.project_id = :project_id "
    "  AND (CAST(:severity AS text) IS NULL OR f.severity = CAST(:severity AS text)) "
    "  AND (CAST(:category AS text) IS NULL OR f.category = CAST(:category AS text)) "
    "  AND (CAST(:verdict AS text) IS NULL OR f.verdict = CAST(:verdict AS text)) "
    "  AND (CAST(:resolved AS boolean) IS NULL "
    "       OR (f.resolved_at IS NOT NULL) = CAST(:resolved AS boolean)) "
    # Blocked attacks are findings too (a recorded pass), but triage is about
    # what went wrong unless the caller asks for verdict=blocked explicitly.
    "  AND (CAST(:verdict AS text) IS NOT NULL OR f.verdict <> 'blocked') "
    "ORDER BY f.created_at DESC LIMIT :limit"
)
_ONE_SQL = text(
    f"SELECT {FINDING_COLUMNS}, r.project_id FROM findings f JOIN runs r ON r.id = f.run_id "
    "WHERE f.id = :id"
)


@router.get("/v1/findings", response_model=list[FindingSummary])
async def list_findings(
    severity: Literal["critical", "high", "medium", "low"] | None = None,
    category: Literal["Injection", "PII", "Auth", "Social", "Harmful"] | None = None,
    verdict: Literal["blocked", "leaked", "bypassed"] | None = None,
    resolved: bool | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
) -> list[FindingSummary]:
    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    _LIST_SQL,
                    {
                        "project_id": project_id,
                        "severity": severity,
                        "category": category,
                        "verdict": verdict,
                        "resolved": resolved,
                        "limit": limit,
                    },
                )
            )
            .mappings()
            .all()
        )
    return [finding_summary(r) for r in rows]


@router.get("/v1/findings/{finding_id}", response_model=FindingDetail)
async def get_finding(
    finding_id: uuid.UUID,
    user_id: str = Depends(require_user_id),
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
) -> FindingDetail:
    async with engine.connect() as conn, conn.begin():
        row = (await conn.execute(_ONE_SQL, {"id": finding_id})).mappings().first()
        if row is None:
            raise APIError("not_found", "finding not found", status.HTTP_404_NOT_FOUND)
        ensure_project_match(row["project_id"], project_id)
        (detail,) = await reveal_findings(conn, [row], user_id=user_id)
    return detail


@router.patch("/v1/findings/{finding_id}", response_model=FindingSummary)
async def resolve_finding(
    finding_id: uuid.UUID,
    body: FindingResolve,
    _user_id: str = Depends(require_user_id),
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
) -> FindingSummary:
    async with engine.connect() as conn, conn.begin():
        row = (await conn.execute(_ONE_SQL, {"id": finding_id})).mappings().first()
        if row is None:
            raise APIError("not_found", "finding not found", status.HTTP_404_NOT_FOUND)
        ensure_project_match(row["project_id"], project_id)
        await conn.execute(
            text(
                "UPDATE findings SET resolved_at = "
                "CASE WHEN CAST(:resolved AS boolean) THEN COALESCE(resolved_at, now()) "
                "ELSE NULL END WHERE id = :id"
            ),
            {"id": finding_id, "resolved": body.resolved},
        )
        updated = (await conn.execute(_ONE_SQL, {"id": finding_id})).mappings().one()
    return finding_summary(updated)
