"""B3-03 tests: concurrency enforcement at claim time.

The B1-04 pre-check at run-creation time is advisory only; claim_run() is
the real, authoritative gate against `agents.max_concurrency`. Separate
file from tests/test_workers.py (not that file's existing claim tests)
because that file's autouse cleanup fixture currently can't run at all in
this environment (a pre-existing, unrelated FK conflict against seeded
data) -- these tests scope their own cleanup by agent id instead so they
can actually be verified.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.workers.claim import claim_run
from app.workers.fake_runner import run_fake_script
from tests.conftest import _test_engine, requires_test_db

pytestmark = requires_test_db


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = _test_engine()
    yield eng
    await eng.dispose()


async def _seed_agent(conn: AsyncConnection, *, max_concurrency: int) -> uuid.UUID:
    agent_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO agents (id, name, transport, max_concurrency, created_by_user_id) "
            "VALUES (:id, 'Claim Concurrency Agent', 'web', :max_concurrency, 'user-1')"
        ),
        {"id": agent_id, "max_concurrency": max_concurrency},
    )
    return agent_id


async def _seed_queued_run(
    conn: AsyncConnection, agent_id: uuid.UUID, *, parent_id: uuid.UUID | None = None
) -> uuid.UUID:
    run_id = uuid.uuid4()
    # created_at uses clock_timestamp(), not the runs table's now()-based
    # server_default: now()/transaction_timestamp() is frozen for the whole
    # transaction, so seeding several rows back-to-back inside one `conn.begin()`
    # (as the FIFO-ordering tests here do) would otherwise give them all the
    # exact same created_at and make claim order nondeterministic on the tie.
    await conn.execute(
        text(
            "INSERT INTO runs (id, type, agent_id, parent_run_id, created_by_user_id, "
            " created_at) "
            "VALUES (:id, 'simulation', :agent_id, :parent_id, 'user-1', clock_timestamp())"
        ),
        {"id": run_id, "agent_id": agent_id, "parent_id": parent_id},
    )
    return run_id


async def _live_statuses(engine: AsyncEngine, agent_id: uuid.UUID) -> list[str]:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT status FROM runs WHERE agent_id = :id ORDER BY created_at"),
                {"id": agent_id},
            )
        ).scalars().all()
    return list(rows)


# Every table with a FK to runs.id that a real executor (run_fake_script,
# via materialize_run) can populate -- not just run_events. Missing any of
# these surfaces as an FK violation on the final `DELETE FROM runs` below,
# not on the table actually missing a cleanup, so keep this list in sync
# with app/models/runs.py's `ForeignKey("runs.id")` columns.
_RUN_CHILD_TABLES = ("run_events", "turns", "assertion_results", "findings")


async def _cleanup(engine: AsyncEngine, agent_ids: list[uuid.UUID]) -> None:
    async with engine.connect() as conn, conn.begin():
        for agent_id in agent_ids:
            for table in _RUN_CHILD_TABLES:
                await conn.execute(
                    text(
                        f"DELETE FROM {table} "
                        "WHERE run_id IN (SELECT id FROM runs WHERE agent_id = :id)"
                    ),
                    {"id": agent_id},
                )
            await conn.execute(text("DELETE FROM runs WHERE agent_id = :id"), {"id": agent_id})
            await conn.execute(text("DELETE FROM agents WHERE id = :id"), {"id": agent_id})
    await engine.dispose()


async def test_serial_claims_never_exceed_max_concurrency_one(engine: AsyncEngine) -> None:
    async with engine.connect() as conn, conn.begin():
        agent_id = await _seed_agent(conn, max_concurrency=1)
        run_ids = [await _seed_queued_run(conn, agent_id) for _ in range(3)]
    try:
        for expected_id in run_ids:
            claimed = await claim_run(engine, "serial-worker")
            assert claimed is not None
            assert claimed.id == expected_id

            # No further claim possible while this one is still live.
            assert await claim_run(engine, "serial-worker") is None

            # "Finish" it before claiming the next.
            async with engine.connect() as conn, conn.begin():
                await conn.execute(
                    text("UPDATE runs SET status = 'completed' WHERE id = :id"),
                    {"id": claimed.id},
                )
    finally:
        await _cleanup(engine, [agent_id])


async def test_concurrent_claims_never_exceed_max_concurrency(engine: AsyncEngine) -> None:
    """The race B3-03 exists to close: a plain COUNT-then-UPDATE lets two
    concurrent claimers both read '0 live' before either commits. Fires 10
    concurrent claim_run calls against one agent capped at 3.

    The per-agent lock is a try-lock (non-blocking, to avoid deadlocking
    concurrent multi-agent scans against each other -- see claim.py), so a
    single concurrent burst may claim fewer than the full cap: losers skip
    rather than wait. The invariants that must hold are "never more than
    max_concurrency claimed" and "no double-claim" -- checked here -- plus
    "the cap is still reachable", checked below by draining serially.
    """
    async with engine.connect() as conn, conn.begin():
        agent_id = await _seed_agent(conn, max_concurrency=3)
        run_ids = [await _seed_queued_run(conn, agent_id) for _ in range(10)]
    try:
        results = await asyncio.gather(*(claim_run(engine, f"worker-{i}") for i in range(10)))
        successes = [r for r in results if r is not None]
        assert len(successes) <= 3
        assert len({r.id for r in successes}) == len(successes)  # no double-claim
        assert {r.id for r in successes}.issubset(set(run_ids))

        # Drain serially: any burst losers must still be claimable, and the
        # cap must still be exactly reachable once contention is gone.
        claimed_total = len(successes)
        while await claim_run(engine, "drain-worker") is not None:
            claimed_total += 1
        assert claimed_total == 3

        statuses = await _live_statuses(engine, agent_id)
        assert statuses.count("claimed") == 3
        assert statuses.count("queued") == 7
    finally:
        await _cleanup(engine, [agent_id])


async def test_capacity_check_counts_running_not_just_claimed(engine: AsyncEngine) -> None:
    async with engine.connect() as conn, conn.begin():
        agent_id = await _seed_agent(conn, max_concurrency=1)
        running_id = await _seed_queued_run(conn, agent_id)
        await conn.execute(
            text("UPDATE runs SET status = 'running' WHERE id = :id"), {"id": running_id}
        )
        await _seed_queued_run(conn, agent_id)
    try:
        assert await claim_run(engine, "worker") is None
    finally:
        await _cleanup(engine, [agent_id])


async def test_capped_agent_does_not_block_a_different_agents_claim(engine: AsyncEngine) -> None:
    async with engine.connect() as conn, conn.begin():
        capped_agent = await _seed_agent(conn, max_concurrency=1)
        capped_run = await _seed_queued_run(conn, capped_agent)
        await conn.execute(
            text("UPDATE runs SET status = 'claimed' WHERE id = :id"), {"id": capped_run}
        )
        # Capped agent's queued run is older (created first) -- would win a
        # naive "oldest queued row, no capacity check" claim.
        await conn.execute(
            text("UPDATE runs SET created_at = now() - interval '1 minute' WHERE id = :id"),
            {"id": capped_run},
        )
        blocked_queued = await _seed_queued_run(conn, capped_agent)

        open_agent = await _seed_agent(conn, max_concurrency=1)
        open_run = await _seed_queued_run(conn, open_agent)
    try:
        claimed = await claim_run(engine, "worker")
        assert claimed is not None
        assert claimed.id == open_run
        assert claimed.agent_id == open_agent

        status = await _live_statuses(engine, capped_agent)
        assert status == ["claimed", "queued"]  # blocked_queued untouched
        _ = blocked_queued
    finally:
        await _cleanup(engine, [capped_agent, open_agent])


async def test_claimed_run_executes_through_fake_runner(engine: AsyncEngine) -> None:
    """B3-03 rewrote the claim query (candidate selection, locking, WHERE
    clause) -- this is the one runnable check left that claim_run's output
    still feeds a real executor correctly end to end. (tests/test_workers.py
    covers the same path but its autouse cleanup fixture can't run here --
    see module docstring -- so it's unrunnable, not redundant.)"""
    async with engine.connect() as conn, conn.begin():
        agent_id = await _seed_agent(conn, max_concurrency=1)
        run_id = await _seed_queued_run(conn, agent_id)
    try:
        claimed = await claim_run(engine, "worker")
        assert claimed is not None
        assert claimed.id == run_id

        await run_fake_script(engine, claimed)

        statuses = await _live_statuses(engine, agent_id)
        assert statuses == ["completed"]
    finally:
        await _cleanup(engine, [agent_id])


async def test_claim_run_returns_none_with_no_queued_runs(engine: AsyncEngine) -> None:
    async with engine.connect() as conn, conn.begin():
        agent_id = await _seed_agent(conn, max_concurrency=5)
    try:
        assert await claim_run(engine, "worker") is None
    finally:
        await _cleanup(engine, [agent_id])


async def test_claimed_run_carries_parent_run_id_for_a_batch_child(engine: AsyncEngine) -> None:
    """B2.6-05: ClaimedRun.parent_run_id is what the real executor
    (app.engine.executor.simulation) uses to tag every span it creates --
    this is the claim-side half of that plumbing."""
    async with engine.connect() as conn, conn.begin():
        agent_id = await _seed_agent(conn, max_concurrency=5)
        parent_id = uuid.uuid4()
        await conn.execute(
            text(
                "INSERT INTO runs (id, type, status, agent_id, created_by_user_id) "
                "VALUES (:id, 'suite', 'queued', :agent_id, 'user-1')"
            ),
            {"id": parent_id, "agent_id": agent_id},
        )
        child_id = await _seed_queued_run(conn, agent_id, parent_id=parent_id)
    try:
        claimed = await claim_run(engine, "worker")
        assert claimed is not None
        assert claimed.id == child_id
        assert claimed.parent_run_id == parent_id
    finally:
        await _cleanup(engine, [agent_id])


async def test_claimed_run_parent_run_id_is_none_for_a_standalone_run(
    engine: AsyncEngine,
) -> None:
    async with engine.connect() as conn, conn.begin():
        agent_id = await _seed_agent(conn, max_concurrency=5)
        await _seed_queued_run(conn, agent_id)
    try:
        claimed = await claim_run(engine, "worker")
        assert claimed is not None
        assert claimed.parent_run_id is None
    finally:
        await _cleanup(engine, [agent_id])
