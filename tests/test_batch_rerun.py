"""B2.6-04 tests: POST /v1/runs/{id}/rerun?only=failed|all.

Same endpoint as B1-05's original single-run rerun (there's no separate
{parentId} route) -- branches on whether `id` has children, mirroring
B2.6-02's cancel cascade. Leaf-rerun's pre-existing behavior (clone one
run's config into a new run) is covered by tests/test_run_lifecycle.py;
this file covers the batch-clone branch plus rerun_of_run_id on both
paths.
"""

import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.config import get_settings
from app.main import app
from tests.conftest import _test_engine, auth_headers, requires_test_db

pytestmark = requires_test_db

_HEADERS = auth_headers()


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_HEADERS)


async def _make_agent(engine: AsyncEngine, name: str = "Rerun Agent") -> uuid.UUID:
    agent_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO agents (id, name, transport, created_by_user_id) "
                "VALUES (:id, :name, 'web', 'user-1')"
            ),
            {"id": agent_id, "name": name},
        )
    return agent_id


async def _seed_parent(conn: AsyncConnection, agent_id: uuid.UUID) -> uuid.UUID:
    parent_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO runs (id, type, status, agent_id, created_by_user_id) "
            "VALUES (:id, 'suite', 'completed', :agent_id, 'user-1')"
        ),
        {"id": parent_id, "agent_id": agent_id},
    )
    return parent_id


async def _seed_child(
    conn: AsyncConnection,
    agent_id: uuid.UUID,
    parent_id: uuid.UUID,
    *,
    status: str = "completed",
    result_badge: str | None = "pass",
) -> uuid.UUID:
    child_id = uuid.uuid4()
    metrics = json.dumps({"resultBadge": result_badge}) if result_badge else "null"
    await conn.execute(
        text(
            "INSERT INTO runs (id, type, status, agent_id, parent_run_id, created_by_user_id, "
            " metrics) "
            "VALUES (:id, 'simulation', :status, :agent_id, :parent_id, 'user-1', "
            " CAST(:metrics AS jsonb))"
        ),
        {
            "id": child_id,
            "status": status,
            "agent_id": agent_id,
            "parent_id": parent_id,
            "metrics": metrics,
        },
    )
    return child_id


_RUNS_SQL = text(
    "SELECT id, type, status, agent_id, scenario_id, parent_run_id, rerun_of_run_id "
    "FROM runs WHERE id IN :ids"
).bindparams(bindparam("ids", expanding=True))


async def _runs(
    engine: AsyncEngine, run_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[str, object]]:
    async with engine.connect() as conn:
        rows = (await conn.execute(_RUNS_SQL, {"ids": run_ids})).mappings().all()
    return {row["id"]: dict(row) for row in rows}


async def _children_of(engine: AsyncEngine, parent_id: uuid.UUID) -> list[dict[str, object]]:
    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text("SELECT id, scenario_id FROM runs WHERE parent_run_id = :id"),
                    {"id": parent_id},
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


async def _cleanup(engine: AsyncEngine, agent_id: uuid.UUID) -> None:
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "DELETE FROM run_events "
                "WHERE run_id IN (SELECT id FROM runs WHERE agent_id = :id)"
            ),
            {"id": agent_id},
        )
        await conn.execute(text("DELETE FROM runs WHERE agent_id = :id"), {"id": agent_id})
        await conn.execute(text("DELETE FROM agents WHERE id = :id"), {"id": agent_id})
    await engine.dispose()


async def test_rerun_leaf_run_clones_config_and_records_lineage() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine)
    async with engine.connect() as conn, conn.begin():
        run_id = (
            await conn.execute(
                text(
                    "INSERT INTO runs (id, type, status, agent_id, created_by_user_id) "
                    "VALUES (:id, 'simulation', 'completed', :agent_id, 'user-1') RETURNING id"
                ),
                {"id": uuid.uuid4(), "agent_id": agent_id},
            )
        ).scalar_one()
    try:
        async with await _client() as client:
            response = await client.post(f"/v1/runs/{run_id}/rerun")
        assert response.status_code == 202
        new_run_id = uuid.UUID(response.json()["runId"])

        rows = await _runs(engine, [new_run_id])
        assert rows[new_run_id]["rerun_of_run_id"] == run_id
        assert rows[new_run_id]["status"] == "queued"
    finally:
        await _cleanup(engine, agent_id)


async def test_rerun_batch_only_all_clones_every_child() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine, "Rerun All Agent")
    async with engine.connect() as conn, conn.begin():
        parent_id = await _seed_parent(conn, agent_id)
        for _ in range(4):
            await _seed_child(conn, agent_id, parent_id, status="completed", result_badge="pass")
        await _seed_child(conn, agent_id, parent_id, status="failed", result_badge=None)
    try:
        async with await _client() as client:
            response = await client.post(f"/v1/runs/{parent_id}/rerun", params={"only": "all"})
        assert response.status_code == 202
        body = response.json()
        assert body["callCount"] == 5
        new_parent_id = uuid.UUID(body["parentRunId"])

        rows = await _runs(engine, [new_parent_id])
        assert rows[new_parent_id]["type"] == "suite"
        assert rows[new_parent_id]["rerun_of_run_id"] == parent_id
        assert len(await _children_of(engine, new_parent_id)) == 5
    finally:
        await _cleanup(engine, agent_id)


async def test_rerun_batch_only_failed_clones_just_failing_children() -> None:
    """The ticket's own 'Done when', at a scale this test suite can afford:
    a batch with some failures reruns only those. 'failed' means
    verdict_for_run == 'fail' -- status='failed' AND a 'completed' run
    with resultBadge='fail' both count; a 'completed'/'pass' run doesn't."""
    engine = _test_engine()
    agent_id = await _make_agent(engine, "Rerun Failed Agent")
    async with engine.connect() as conn, conn.begin():
        parent_id = await _seed_parent(conn, agent_id)
        passing = [
            await _seed_child(conn, agent_id, parent_id, status="completed", result_badge="pass")
            for _ in range(3)
        ]
        failed_status = await _seed_child(
            conn, agent_id, parent_id, status="failed", result_badge=None
        )
        failed_badge = await _seed_child(
            conn, agent_id, parent_id, status="completed", result_badge="fail"
        )
    try:
        async with await _client() as client:
            response = await client.post(f"/v1/runs/{parent_id}/rerun", params={"only": "failed"})
        assert response.status_code == 202
        body = response.json()
        assert body["callCount"] == 2
        new_parent_id = uuid.UUID(body["parentRunId"])

        new_children = await _children_of(engine, new_parent_id)
        assert len(new_children) == 2
        _ = passing  # untouched -- not part of the new batch, no direct assertion needed
        _ = failed_status, failed_badge
    finally:
        await _cleanup(engine, agent_id)


async def test_rerun_batch_only_failed_with_no_failures_422s() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine, "No Failures Agent")
    async with engine.connect() as conn, conn.begin():
        parent_id = await _seed_parent(conn, agent_id)
        await _seed_child(conn, agent_id, parent_id, status="completed", result_badge="pass")
    try:
        async with await _client() as client:
            response = await client.post(f"/v1/runs/{parent_id}/rerun", params={"only": "failed"})
        assert response.status_code == 422
    finally:
        await _cleanup(engine, agent_id)


async def test_rerun_batch_over_cap_422s(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "suite_run_batch_cap", 2)
    engine = _test_engine()
    agent_id = await _make_agent(engine, "Rerun Cap Agent")
    async with engine.connect() as conn, conn.begin():
        parent_id = await _seed_parent(conn, agent_id)
        for _ in range(3):
            await _seed_child(conn, agent_id, parent_id, status="completed", result_badge="pass")
    try:
        async with await _client() as client:
            response = await client.post(f"/v1/runs/{parent_id}/rerun", params={"only": "all"})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "batch_too_large"
    finally:
        await _cleanup(engine, agent_id)


async def test_rerun_missing_run_404s() -> None:
    async with await _client() as client:
        response = await client.post(f"/v1/runs/{uuid.uuid4()}/rerun")
    assert response.status_code == 404


async def test_rerun_batch_with_live_children_409s() -> None:
    """Rerunning while the source batch is still executing would double up
    queued work against the same agent for calls already in flight."""
    engine = _test_engine()
    agent_id = await _make_agent(engine, "Live Children Agent")
    async with engine.connect() as conn, conn.begin():
        parent_id = await _seed_parent(conn, agent_id)
        await _seed_child(conn, agent_id, parent_id, status="completed", result_badge="pass")
        await _seed_child(conn, agent_id, parent_id, status="running", result_badge=None)
    try:
        async with await _client() as client:
            response = await client.post(f"/v1/runs/{parent_id}/rerun", params={"only": "all"})
        assert response.status_code == 409
    finally:
        await _cleanup(engine, agent_id)


async def test_rerun_batch_agent_in_another_project_404s() -> None:
    """B2.5-01: a batch whose agent belongs to another project must 404
    exactly like a nonexistent agent -- the batch-rerun path bypasses
    _create_run entirely, so this scoping check has to be explicit here."""
    engine = _test_engine()
    other_project_id = uuid.uuid4()
    other_agent_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text("INSERT INTO projects (id, name) VALUES (:id, 'Other Rerun Project')"),
            {"id": other_project_id},
        )
        await conn.execute(
            text(
                "INSERT INTO agents (id, project_id, name, transport, created_by_user_id) "
                "VALUES (:id, :project_id, 'Other Project Agent', 'web', 'user-1')"
            ),
            {"id": other_agent_id, "project_id": other_project_id},
        )
        # Parent/children physically live in the (default) project the test
        # client is scoped to, but point at an agent from another project --
        # an inconsistency this check exists to catch regardless of how it
        # could arise.
        parent_id = await _seed_parent(conn, other_agent_id)
        await _seed_child(conn, other_agent_id, parent_id, status="completed", result_badge="pass")
    try:
        async with await _client() as client:
            response = await client.post(f"/v1/runs/{parent_id}/rerun", params={"only": "all"})
        assert response.status_code == 404
    finally:
        async with engine.connect() as conn, conn.begin():
            await conn.execute(
                text("DELETE FROM runs WHERE agent_id = :id"), {"id": other_agent_id}
            )
            await conn.execute(
                text("DELETE FROM agents WHERE id = :id"), {"id": other_agent_id}
            )
            await conn.execute(
                text("DELETE FROM projects WHERE id = :id"), {"id": other_project_id}
            )
        await engine.dispose()
