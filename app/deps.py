import uuid

from fastapi import Depends, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import get_engine
from app.errors import APIError


def require_user_id(request: Request) -> str:
    """FastAPI dependency for write endpoints — fail loudly instead of creating
    ownerless rows when the BFF forgets to forward X-User-Id (spine §0)."""
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise APIError(
            code="missing_user_id",
            message="X-User-Id header is required for this endpoint",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return str(user_id)


# B2.5-01: routes that do NOT take `Depends(require_project_id)`, and why.
# "Doesn't take the header" is not the same claim as "needs no
# authorization" -- say what (if anything) scopes each one instead, so
# this list can't be misread as a clean bill of health. Keep it and its
# reasons current: stitch protocol §6 names a missed endpoint as a
# cross-tenant data leak, not a cosmetic bug.
#   - GET /v1/projects, POST /v1/projects, GET /v1/organizations: the
#     switcher calls these to learn which projects/orgs exist *before* one
#     is picked; requiring the header makes the switcher unbootstrappable.
#     Scoped by caller's org_memberships instead (B2.5-02).
#   - PATCH/DELETE /v1/projects/{project_id}: operate ON a project, so
#     X-Project-Id (which names the *working* project) doesn't apply here.
#     Scoped inline in app/api/projects.py's _ensure_project_visible
#     (same org_memberships join require_project_id uses) rather than via
#     this dependency, since the project id comes from the path, not the
#     header.
#   - /v1/healthz: already exempt at the service-token layer.
#   - GET /v1/runs/{id}/stream: browser EventSource cannot set custom
#     request headers, so this cannot *require* X-Project-Id. It's
#     enforced when present (e.g. a BFF proxy can attach it server-side)
#     and permissive when absent -- see app/api/runs.py's stream_run /
#     _run_visible.
#   - Dashboard v1 (app/api/dashboard.py) and personas: pre-B2.5 shapes
#     kept alive for one release per B2.8-01's ticket text; scoped when
#     B2.8 replaces them, not retrofitted here.


_PROJECT_VISIBLE_TO_CALLER_SQL = text(
    "SELECT 1 FROM projects p "
    "JOIN org_memberships m ON m.org_id = p.org_id AND m.user_id = :user_id "
    "WHERE p.id = :project_id AND p.deleted_at IS NULL"
)


async def require_project_id(
    request: Request,
    user_id: str = Depends(require_user_id),
    engine: AsyncEngine = Depends(get_engine),
) -> uuid.UUID:
    """The single scoping choke point (B2.5-01, tightened in B2.5-02):
    parses X-Project-Id, 422s if it's missing or not a UUID, then 404s if
    the project doesn't exist, is soft-deleted, or -- as of B2.5-02 --
    the caller has no `org_memberships` row for its org. All three cases
    return the identical 404, per the "404, not 403" rule: a project you
    can't see must look exactly like a project that was never created.

    Depending on `require_user_id` here (not just on write routes that
    already declare it) is deliberate: a route can't ask "is this project
    visible to the caller" without knowing who the caller is, so every
    project-scoped route -- reads included -- now also requires X-User-Id.
    """
    raw = getattr(request.state, "project_id", None)
    if not raw:
        raise APIError(
            code="missing_project_id",
            message="X-Project-Id header is required for this endpoint",
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    try:
        project_id = uuid.UUID(str(raw))
    except ValueError as exc:
        raise APIError(
            code="invalid_project_id",
            message="X-Project-Id must be a valid UUID",
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        ) from exc

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                _PROJECT_VISIBLE_TO_CALLER_SQL, {"project_id": project_id, "user_id": user_id}
            )
        ).first()
    if row is None:
        raise APIError("not_found", "project not found", status.HTTP_404_NOT_FOUND)
    return project_id


def ensure_project_match(row_project_id: object, expected_project_id: uuid.UUID) -> None:
    """Cross-project 404 for single-resource GETs (B2.5-01's named "Done
    when": a run/suite/agent belonging to another project 404s, not 403 --
    identical shape to a resource that doesn't exist at all)."""
    if str(row_project_id) != str(expected_project_id):
        raise APIError("not_found", "resource not found", status.HTTP_404_NOT_FOUND)
