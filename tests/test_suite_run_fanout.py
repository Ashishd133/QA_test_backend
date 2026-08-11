"""B2.6-01 tests: POST /v1/suites/{id}/run fan-out.

Covers the ticket's "Done when": a suite creates one parent + the scenario
cross-product in one transaction, a repeated Idempotency-Key returns the
original batch rather than queueing a second one, an over-cap request 422s
as batch_too_large, the not-yet-supported personaIds/conditionProfileIds
fields 422 rather than being silently dropped, and -- the one that actually
needs a real claim+execute loop, not just the insert path -- a batch's
children claim and run at exactly `agent.max_concurrency` in parallel.
B3-03's own gate is tested in isolation in tests/test_claim_concurrency.py;
the last test here drives claim_run() + run_fake_script() against this
endpoint's real output instead of hand-seeded rows, deliberately NOT
against app.workers.main's `_claim_loop` (single-replica, strictly
sequential -- that's a deployment-topology fact, not a bug this ticket
owns; see PR discussion).
"""

import asyncio
import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import get_settings
from app.main import app
from app.workers.claim import claim_run
from app.workers.fake_runner import run_fake_script
from tests.conftest import _test_engine, auth_headers, requires_test_db

pytestmark = requires_test_db

_HEADERS = auth_headers()


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_HEADERS)


async def _make_agent(engine: AsyncEngine, name: str = "Fanout Agent") -> uuid.UUID:
    agent_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO agents (id, name, transport, max_concurrency, created_by_user_id) "
                "VALUES (:id, :name, 'web', 5, 'user-1')"
            ),
            {"id": agent_id, "name": name},
        )
    return agent_id


async def _make_suite(
    engine: AsyncEngine, agent_id: uuid.UUID, name: str = "Fanout Suite"
) -> uuid.UUID:
    suite_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO suites (id, name, description, agent_id, created_by_user_id) "
                "VALUES (:id, :name, 'desc', :agent_id, 'user-1')"
            ),
            {"id": suite_id, "name": name, "agent_id": agent_id},
        )
    return suite_id


async def _make_scenario(engine: AsyncEngine, suite_id: uuid.UUID, name: str) -> uuid.UUID:
    scenario_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO scenarios "
                "(id, suite_id, name, persona, persona_initials, assertions, source) "
                "VALUES (:id, :suite_id, :name, 'Priya', 'PR', CAST(:assertions AS jsonb), "
                "'manual')"
            ),
            {"id": scenario_id, "suite_id": suite_id, "name": name, "assertions": json.dumps([])},
        )
    return scenario_id


async def _cleanup(engine: AsyncEngine, *, agent_id: uuid.UUID) -> None:
    async with engine.connect() as conn, conn.begin():
        await conn.execute(text("DELETE FROM runs WHERE agent_id = :id"), {"id": agent_id})
        await conn.execute(text("DELETE FROM suites WHERE agent_id = :id"), {"id": agent_id})
        await conn.execute(text("DELETE FROM agents WHERE id = :id"), {"id": agent_id})
    await engine.dispose()


# Every table with a FK to runs.id that a real executor can populate -- see
# tests/test_claim_concurrency.py's identical list/comment. Only the
# concurrency-integration test below actually runs one; the insert-path
# tests above never execute a claimed run, so plain `_cleanup` covers them.
_RUN_CHILD_TABLES = ("run_events", "turns", "assertion_results", "findings")


async def _cleanup_after_execution(engine: AsyncEngine, *, agent_id: uuid.UUID) -> None:
    async with engine.connect() as conn, conn.begin():
        for table in _RUN_CHILD_TABLES:
            await conn.execute(
                text(
                    f"DELETE FROM {table} "
                    "WHERE run_id IN (SELECT id FROM runs WHERE agent_id = :id)"
                ),
                {"id": agent_id},
            )
        await conn.execute(text("DELETE FROM runs WHERE agent_id = :id"), {"id": agent_id})
        await conn.execute(text("DELETE FROM suites WHERE agent_id = :id"), {"id": agent_id})
        await conn.execute(text("DELETE FROM agents WHERE id = :id"), {"id": agent_id})
    await engine.dispose()


async def test_suite_run_creates_one_parent_plus_scenario_cross_product() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine)
    suite_id = await _make_suite(engine, agent_id)
    scenario_ids = {await _make_scenario(engine, suite_id, f"Scenario {i}") for i in range(4)}
    try:
        async with await _client() as client:
            response = await client.post(
                f"/v1/suites/{suite_id}/run", json={"agentId": str(agent_id)}
            )
        assert response.status_code == 202
        body = response.json()
        assert body["callCount"] == 4
        parent_id = uuid.UUID(body["parentRunId"])

        async with engine.connect() as conn:
            parent = (
                (
                    await conn.execute(
                        text("SELECT type, status, parent_run_id FROM runs WHERE id = :id"),
                        {"id": parent_id},
                    )
                )
                .mappings()
                .one()
            )
            assert parent["type"] == "suite"
            assert parent["status"] == "queued"
            assert parent["parent_run_id"] is None

            children = (
                (
                    await conn.execute(
                        text(
                            "SELECT type, status, scenario_id, agent_id FROM runs "
                            "WHERE parent_run_id = :id"
                        ),
                        {"id": parent_id},
                    )
                )
                .mappings()
                .all()
            )
        assert len(children) == 4
        assert {c["scenario_id"] for c in children} == scenario_ids
        assert all(c["type"] == "simulation" for c in children)
        assert all(c["status"] == "queued" for c in children)
        assert all(c["agent_id"] == agent_id for c in children)
    finally:
        await _cleanup(engine, agent_id=agent_id)


async def test_suite_run_omitting_scenario_ids_runs_every_scenario() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine, "Omit Agent")
    suite_id = await _make_suite(engine, agent_id, "Omit Suite")
    for i in range(3):
        await _make_scenario(engine, suite_id, f"Omit Scenario {i}")
    try:
        async with await _client() as client:
            response = await client.post(
                f"/v1/suites/{suite_id}/run", json={"agentId": str(agent_id)}
            )
        assert response.status_code == 202
        assert response.json()["callCount"] == 3
    finally:
        await _cleanup(engine, agent_id=agent_id)


async def test_suite_run_scoped_to_scenario_ids_subset() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine, "Subset Agent")
    suite_id = await _make_suite(engine, agent_id, "Subset Suite")
    keep = await _make_scenario(engine, suite_id, "Keep")
    await _make_scenario(engine, suite_id, "Drop")
    try:
        async with await _client() as client:
            response = await client.post(
                f"/v1/suites/{suite_id}/run",
                json={"agentId": str(agent_id), "scenarioIds": [str(keep)]},
            )
        assert response.status_code == 202
        assert response.json()["callCount"] == 1

        async with engine.connect() as conn:
            child_scenario = (
                await conn.execute(
                    text("SELECT scenario_id FROM runs WHERE parent_run_id = :id"),
                    {"id": uuid.UUID(response.json()["parentRunId"])},
                )
            ).scalar_one()
        assert child_scenario == keep
    finally:
        await _cleanup(engine, agent_id=agent_id)


async def test_suite_run_idempotency_key_returns_original_batch() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine, "Idempotent Agent")
    suite_id = await _make_suite(engine, agent_id, "Idempotent Suite")
    await _make_scenario(engine, suite_id, "Only Scenario")
    key = f"fanout-{uuid.uuid4()}"
    try:
        async with await _client() as client:
            first = await client.post(
                f"/v1/suites/{suite_id}/run",
                json={"agentId": str(agent_id)},
                headers={"Idempotency-Key": key},
            )
            second = await client.post(
                f"/v1/suites/{suite_id}/run",
                json={"agentId": str(agent_id)},
                headers={"Idempotency-Key": key},
            )
        assert first.status_code == 202
        assert second.status_code == 202
        assert first.json()["parentRunId"] == second.json()["parentRunId"]
        assert second.json()["callCount"] == 1

        async with engine.connect() as conn:
            parent_count = (
                await conn.execute(
                    text("SELECT count(*) FROM runs WHERE type = 'suite' AND agent_id = :id"),
                    {"id": agent_id},
                )
            ).scalar_one()
        assert parent_count == 1  # no second batch queued
    finally:
        await _cleanup(engine, agent_id=agent_id)


async def test_suite_run_over_cap_returns_422_batch_too_large(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "suite_run_batch_cap", 2)
    engine = _test_engine()
    agent_id = await _make_agent(engine, "Cap Agent")
    suite_id = await _make_suite(engine, agent_id, "Cap Suite")
    for i in range(3):
        await _make_scenario(engine, suite_id, f"Cap Scenario {i}")
    try:
        async with await _client() as client:
            response = await client.post(
                f"/v1/suites/{suite_id}/run", json={"agentId": str(agent_id)}
            )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "batch_too_large"

        async with engine.connect() as conn:
            queued = (
                await conn.execute(
                    text("SELECT count(*) FROM runs WHERE agent_id = :id"), {"id": agent_id}
                )
            ).scalar_one()
        assert queued == 0  # rejected before anything was inserted
    finally:
        await _cleanup(engine, agent_id=agent_id)


async def test_suite_run_rejects_non_empty_persona_ids() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine, "Persona Agent")
    suite_id = await _make_suite(engine, agent_id, "Persona Suite")
    await _make_scenario(engine, suite_id, "Scenario")
    try:
        async with await _client() as client:
            response = await client.post(
                f"/v1/suites/{suite_id}/run",
                json={"agentId": str(agent_id), "personaIds": ["p1"]},
            )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "not_supported"
    finally:
        await _cleanup(engine, agent_id=agent_id)


async def test_suite_run_unknown_scenario_id_404s() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine, "Unknown Agent")
    suite_id = await _make_suite(engine, agent_id, "Unknown Suite")
    await _make_scenario(engine, suite_id, "Scenario")
    try:
        async with await _client() as client:
            response = await client.post(
                f"/v1/suites/{suite_id}/run",
                json={"agentId": str(agent_id), "scenarioIds": [str(uuid.uuid4())]},
            )
        assert response.status_code == 404
    finally:
        await _cleanup(engine, agent_id=agent_id)


# 6 real run_fake_script replays (each ~2.6s of scripted delay, each event
# opening its own connection under null_pool -- see tests/conftest.py's
# _test_engine docstring) against a Neon test DB with observed multi-second
# connection stalls: generous margin over the default 120s.
@pytest.mark.timeout(600)
async def test_suite_run_children_claim_and_run_at_exactly_max_concurrency() -> None:
    """B2.6-01's own 'Done when': a batch's children claim and execute at
    exactly `agent.max_concurrency` in parallel, never more. Drives the
    real claim_run() -> run_fake_script() loop against this endpoint's own
    output (not hand-seeded rows, as tests/test_claim_concurrency.py uses)
    -- more claimer loops than the cap, and each loop keeps polling on a
    `None` claim rather than exiting (mirrors app.workers.main's
    `_claim_loop`): under try-lock, `None` means "this agent's queue is
    drained" OR "lost the race this instant", indistinguishable from the
    caller's side, so treating it as "done" would abandon a still-queued
    agent to whichever one claimer got lucky first -- exactly the claim.py
    bug this test caught (see its docstring)."""
    engine = _test_engine()
    agent_id = await _make_agent(engine, "Max Concurrency Agent")
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text("UPDATE agents SET max_concurrency = 3 WHERE id = :id"), {"id": agent_id}
        )
    suite_id = await _make_suite(engine, agent_id, "Max Concurrency Suite")
    for i in range(6):
        await _make_scenario(engine, suite_id, f"Max Concurrency Scenario {i}")

    try:
        async with await _client() as client:
            response = await client.post(
                f"/v1/suites/{suite_id}/run", json={"agentId": str(agent_id)}
            )
        assert response.status_code == 202
        assert response.json()["callCount"] == 6
        parent_id = uuid.UUID(response.json()["parentRunId"])

        max_observed = 0
        stop = asyncio.Event()
        _TERMINAL = ("completed", "cancelled", "failed")

        async def _claimer(worker_id: str) -> None:
            while not stop.is_set():
                claimed = await claim_run(engine, worker_id)
                if claimed is None:
                    # See docstring: retry rather than exit. `stop` (set by
                    # _watch_and_poll below once every child is terminal)
                    # is what actually ends this loop.
                    await asyncio.sleep(0.2)
                    continue
                await run_fake_script(engine, claimed)

        async def _watch_and_poll() -> None:
            nonlocal max_observed
            # One connection, reused for every sample -- null_pool means a
            # fresh engine.connect() per poll would re-pay the (sometimes
            # multi-second) connection-establishment cost every 250ms.
            #
            # 'claimed' OR 'running' counts as live, not just 'running':
            # claim.py's own capacity gate (_AGENT_CAPACITY_SQL) counts
            # both -- a run sits 'claimed' for however long it takes
            # run_fake_script's *own* first DB write to land (a second,
            # separate connection, itself subject to the same network
            # variance), which is real lag downstream of claim_run's
            # already-committed decision, not a gap in what was actually
            # granted concurrently.
            async with engine.connect() as conn:
                while True:
                    rows = (
                        (
                            await conn.execute(
                                text("SELECT status FROM runs WHERE parent_run_id = :id"),
                                {"id": parent_id},
                            )
                        )
                        .scalars()
                        .all()
                    )
                    max_observed = max(
                        max_observed, sum(1 for s in rows if s in ("claimed", "running"))
                    )
                    if all(s in _TERMINAL for s in rows):
                        stop.set()
                        return
                    await asyncio.sleep(0.25)

        # More claimer loops than the cap on purpose (see docstring).
        await asyncio.gather(
            _watch_and_poll(), *(_claimer(f"fanout-worker-{i}") for i in range(6))
        )

        async with engine.connect() as conn:
            statuses = (
                (
                    await conn.execute(
                        text("SELECT status FROM runs WHERE parent_run_id = :id"),
                        {"id": parent_id},
                    )
                )
                .scalars()
                .all()
            )
            parent_status = (
                await conn.execute(
                    text("SELECT status FROM runs WHERE id = :id"), {"id": parent_id}
                )
            ).scalar_one()
        assert len(statuses) == 6
        assert all(s == "completed" for s in statuses)
        # Rollup (B2.5-04) closes the parent once every child is terminal,
        # even though the parent itself was never claimed/run (it's
        # excluded from claim.py's candidate scan for having children).
        assert parent_status == "completed"
        assert max_observed <= 3
        assert max_observed == 3
    finally:
        await _cleanup_after_execution(engine, agent_id=agent_id)
