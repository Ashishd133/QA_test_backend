"""B2.5-01: route-enumeration scoping test.

Stitch protocol §6 names "project scoping missed on one endpoint" as a
cross-tenant data leak, not a cosmetic bug, and prescribes exactly this: a
test that walks every route and asserts scoping, so the *next* endpoint
someone adds and forgets to scope fails CI instead of shipping. Structural
(no DB, no HTTP round trip) so it runs in every environment and stays fast
enough that nobody skips it.

`app.deps.require_project_id`'s own docstring-adjacent comment is the
source of truth for the exemption list -- keep the two in sync.
"""

import uuid

from fastapi.routing import APIRoute, _IncludedRouter
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.deps import require_project_id
from app.main import app
from app.models.projects import DEFAULT_PROJECT_ID
from tests.conftest import _test_engine, auth_headers, requires_test_db

# Mirrors the exemption list documented above app.deps.require_project_id.
_EXEMPT: set[tuple[str, str]] = {
    ("GET", "/v1/healthz"),
    ("GET", "/v1/runs/{run_id}/stream"),
    ("GET", "/v1/projects"),
    ("POST", "/v1/projects"),
    ("PATCH", "/v1/projects/{project_id}"),
    ("DELETE", "/v1/projects/{project_id}"),
    ("GET", "/v1/organizations"),
    ("GET", "/v1/metrics/dashboard"),
    ("GET", "/v1/personas"),
    ("POST", "/v1/agents/test-connection"),
}


def _all_api_routes() -> list[APIRoute]:
    """FastAPI 0.140's `include_router` wraps included routers in
    `_IncludedRouter` rather than flattening their routes directly into
    `app.routes`, so this walks one level deeper than a plain `app.routes`
    scan would."""
    routes: list[APIRoute] = []
    for route in app.routes:
        if isinstance(route, _IncludedRouter):
            routes.extend(r for r in route.original_router.routes if isinstance(r, APIRoute))
        elif isinstance(route, APIRoute):
            routes.append(route)
    return routes


def test_every_non_exempt_route_requires_project_id() -> None:
    unscoped: list[tuple[str, str]] = []
    for route in _all_api_routes():
        for method in sorted(route.methods or ()):
            if method == "HEAD":
                continue
            key = (method, route.path)
            if key in _EXEMPT:
                continue
            depends_on_project = any(
                dep.call is require_project_id for dep in route.dependant.dependencies
            )
            if not depends_on_project:
                unscoped.append(key)

    assert not unscoped, (
        f"routes missing Depends(require_project_id): {unscoped} -- either scope them or add "
        "them to _EXEMPT here (and to app.deps.require_project_id's exemption comment) with a "
        "written reason"
    )


def test_exempt_list_has_no_stale_or_duplicate_entries() -> None:
    """Catches the opposite mistake: a route renamed/removed but left in
    _EXEMPT, silently hiding that it (or its replacement) is unscoped."""
    live_routes = {
        (method, route.path)
        for route in _all_api_routes()
        for method in (route.methods or ())
        if method != "HEAD"
    }
    stale = _EXEMPT - live_routes
    assert not stale, f"_EXEMPT lists routes that no longer exist: {stale}"


# ---------------------------------------------------------------------------
# Dynamic tests: the structural tests above prove every route *takes*
# Depends(require_project_id); they cannot prove a route that has the
# dependency still forgets `ensure_project_match` on the row it fetched --
# which is exactly how the next leak ships. These exercise B2.5-01's own
# named "Done when" criteria directly, against a second, real project.
# ---------------------------------------------------------------------------

pytestmark = requires_test_db


async def _make_other_project(engine: AsyncEngine) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A second project with one agent, one suite, one run -- everything a
    caller scoped to DEFAULT_PROJECT_ID must not be able to see."""
    project_id, agent_id, suite_id, run_id = (
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
    )
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text("INSERT INTO projects (id, name) VALUES (:id, 'Other Project')"),
            {"id": project_id},
        )
        await conn.execute(
            text(
                "INSERT INTO agents (id, project_id, name, transport, created_by_user_id) "
                "VALUES (:id, :project_id, 'Other Project Agent', 'web', 'user-1')"
            ),
            {"id": agent_id, "project_id": project_id},
        )
        await conn.execute(
            text(
                "INSERT INTO suites (id, project_id, name, agent_id, created_by_user_id) "
                "VALUES (:id, :project_id, 'Other Project Suite', :agent_id, 'user-1')"
            ),
            {"id": suite_id, "project_id": project_id, "agent_id": agent_id},
        )
        await conn.execute(
            text(
                "INSERT INTO runs (id, project_id, type, agent_id, created_by_user_id) "
                "VALUES (:id, :project_id, 'simulation', :agent_id, 'user-1')"
            ),
            {"id": run_id, "project_id": project_id, "agent_id": agent_id},
        )
    return agent_id, suite_id, run_id


async def _cleanup_other_project(engine: AsyncEngine, project_id: uuid.UUID) -> None:
    async with engine.connect() as conn, conn.begin():
        await conn.execute(text("DELETE FROM runs WHERE project_id = :id"), {"id": project_id})
        await conn.execute(text("DELETE FROM suites WHERE project_id = :id"), {"id": project_id})
        await conn.execute(text("DELETE FROM agents WHERE project_id = :id"), {"id": project_id})
        await conn.execute(text("DELETE FROM projects WHERE id = :id"), {"id": project_id})


async def _client(*, project_id: object = DEFAULT_PROJECT_ID) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=auth_headers(project_id=project_id),
    )


async def test_resource_in_another_project_404s_for_run_suite_and_agent() -> None:
    engine = _test_engine()
    agent_id, suite_id, run_id = await _make_other_project(engine)
    try:
        async with await _client() as client:  # caller's own (default) project header
            run_resp = await client.get(f"/v1/runs/{run_id}")
            suite_resp = await client.get(f"/v1/suites/{suite_id}")
            agent_resp = await client.get(f"/v1/agents/{agent_id}")
        for resp in (run_resp, suite_resp, agent_resp):
            assert resp.status_code == 404
            assert resp.json()["error"]["code"] == "not_found"
    finally:
        # project_id captured from the agent row inserted above.
        async with engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT project_id FROM agents WHERE id = :id"), {"id": agent_id}
                    )
                )
                .mappings()
                .one()
            )
        await _cleanup_other_project(engine, row["project_id"])


async def test_write_with_missing_project_id_header_is_422() -> None:
    async with await _client(project_id=None) as client:
        response = await client.post("/v1/agents", json={"name": "X"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "missing_project_id"


async def test_same_idempotency_key_across_projects_creates_two_runs() -> None:
    """Migration 005's UNIQUE(project_id, idempotency_key): the same key
    reused by a second project must not silently return the first
    project's run_id (the bug the composite constraint replaced a global
    one to close)."""
    engine = _test_engine()
    seed_agent_id, _suite_id, _run_id = await _make_other_project(engine)
    default_agent_id = uuid.uuid4()
    other_agent_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO agents (id, name, transport, created_by_user_id) "
                "VALUES (:id, 'Default Project Agent', 'web', 'user-1')"
            ),
            {"id": default_agent_id},
        )
        other_project_id = (
            (
                await conn.execute(
                    text("SELECT project_id FROM agents WHERE id = :id"),
                    {"id": seed_agent_id},
                )
            )
            .mappings()
            .one()
        )["project_id"]
        # A fresh agent in the other project, not `seed_agent_id` --
        # `_make_other_project` already gave that one a queued run, which
        # would consume its (default max_concurrency=1) only slot and mask
        # this test's real assertion behind an unrelated 409.
        await conn.execute(
            text(
                "INSERT INTO agents (id, project_id, name, transport, created_by_user_id) "
                "VALUES (:id, :project_id, 'Other Project Agent 2', 'web', 'user-1')"
            ),
            {"id": other_agent_id, "project_id": other_project_id},
        )

    key = f"cross-project-{uuid.uuid4()}"
    try:
        async with await _client() as client:  # caller's own (default) project
            first = await client.post(
                "/v1/redteam/runs",
                json={"agentId": str(default_agent_id), "categories": ["Injection"]},
                headers={"Idempotency-Key": key},
            )
        async with await _client(project_id=other_project_id) as client:
            second = await client.post(
                "/v1/redteam/runs",
                json={"agentId": str(other_agent_id), "categories": ["Injection"]},
                headers={"Idempotency-Key": key},
            )
        assert first.status_code == 202
        assert second.status_code == 202
        assert first.json()["runId"] != second.json()["runId"]
    finally:
        async with engine.connect() as conn, conn.begin():
            await conn.execute(
                text("DELETE FROM runs WHERE agent_id IN (:a, :b)"),
                {"a": default_agent_id, "b": other_agent_id},
            )
            await conn.execute(text("DELETE FROM agents WHERE id = :id"), {"id": default_agent_id})
        await _cleanup_other_project(engine, other_project_id)


# ---------------------------------------------------------------------------
# B2.5-02: organization membership is the visibility check require_project_id
# and app/api/projects.py now share -- a project in an org the caller has no
# membership row for must 404 exactly like a project that doesn't exist.
# ---------------------------------------------------------------------------


async def _make_other_org_and_project(engine: AsyncEngine) -> tuple[uuid.UUID, uuid.UUID]:
    """A second org, with no membership granted to 'user-1', holding one
    project."""
    org_id, project_id = uuid.uuid4(), uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text("INSERT INTO organizations (id, name, slug) VALUES (:id, 'Other Org', :slug)"),
            {"id": org_id, "slug": f"other-org-{org_id}"},
        )
        await conn.execute(
            text(
                "INSERT INTO projects (id, org_id, name) VALUES (:id, :org_id, 'Other Org Project')"
            ),
            {"id": project_id, "org_id": org_id},
        )
    return org_id, project_id


async def _cleanup_other_org(engine: AsyncEngine, org_id: uuid.UUID) -> None:
    async with engine.connect() as conn, conn.begin():
        await conn.execute(text("DELETE FROM projects WHERE org_id = :id"), {"id": org_id})
        await conn.execute(text("DELETE FROM org_memberships WHERE org_id = :id"), {"id": org_id})
        await conn.execute(text("DELETE FROM organizations WHERE id = :id"), {"id": org_id})


async def test_project_in_org_without_membership_404s() -> None:
    engine = _test_engine()
    org_id, project_id = await _make_other_org_and_project(engine)
    try:
        async with await _client(project_id=project_id) as client:
            response = await client.get("/v1/agents")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
    finally:
        await _cleanup_other_org(engine, org_id)


async def test_list_organizations_returns_default_org_with_nested_projects() -> None:
    async with await _client() as client:
        response = await client.get("/v1/organizations")
    assert response.status_code == 200
    body = response.json()
    slugs = {org["slug"]: org for org in body}
    assert "org_default" in slugs
    default_org = slugs["org_default"]
    assert isinstance(default_org["projects"], list)
    assert any(p["id"] == str(DEFAULT_PROJECT_ID) for p in default_org["projects"])


async def test_list_organizations_empty_for_user_with_no_membership() -> None:
    async with await _client() as client:
        response = await client.get(
            "/v1/organizations", headers=auth_headers(user_id=f"ghost-{uuid.uuid4()}")
        )
    assert response.status_code == 200
    assert response.json() == []
