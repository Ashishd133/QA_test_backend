"""B0-08 (claim loop + FakeRunner) and B0-09 (reaper) tests. All tests seed/
clean up their own rows against the real Neon DB (skipped when
DATABASE_URL isn't configured)."""

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.workers import fake_runner as fake_runner_module
from app.workers.claim import claim_run
from app.workers.executors import execute_run
from app.workers.fake_runner import load_script, run_fake_script
from app.workers.heartbeat import update_heartbeat
from app.workers.reaper import reap_stale_runs, reaper_loop
from tests.conftest import _test_engine, requires_test_db

pytestmark = requires_test_db


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = _test_engine()
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _clear_runs_table(engine: AsyncEngine) -> AsyncIterator[None]:
    """claim_run() picks the globally oldest queued run — these tests are
    sensitive to *any* other queued/claimed row in this shared dev DB, not
    just the ones they seed themselves. A stray row left by an earlier,
    interrupted test run once caused a claim to land on the wrong id and
    made this test file look like it had a real ordering bug (it didn't —
    reproduced the exact same insert/backdate/insert sequence standalone
    against a clean table and it claimed correctly). Clearing before each
    test — safe here since this database exists solely for this project's
    own dev/test use, no other data — removes that whole failure class.
    """
    async with engine.connect() as conn, conn.begin():
        # B1-07 materializes turns/assertion_results on `done` with no
        # ON DELETE CASCADE from runs (they're derived, re-derivable data,
        # not the run_events source of truth -- but the FK still blocks a
        # bare `DELETE FROM runs` once a completed run has left rows here).
        await conn.execute(text("DELETE FROM turns"))
        await conn.execute(text("DELETE FROM assertion_results"))
        await conn.execute(text("DELETE FROM run_events"))
        await conn.execute(text("DELETE FROM runs"))
        await conn.execute(text("DELETE FROM agents"))
    yield


async def _seed_agent(conn: AsyncConnection) -> uuid.UUID:
    agent_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO agents (id, name, transport, created_by_user_id) "
            "VALUES (:id, 'Worker Test Agent', 'web', 'user-1')"
        ),
        {"id": agent_id},
    )
    return agent_id


async def _seed_queued_run(
    conn: AsyncConnection, agent_id: uuid.UUID, run_type: str = "simulation"
) -> uuid.UUID:
    run_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO runs (id, type, agent_id, created_by_user_id) "
            "VALUES (:id, :type, :agent_id, 'user-1')"
        ),
        {"id": run_id, "type": run_type, "agent_id": agent_id},
    )
    return run_id


async def _backdate_heartbeat(engine: AsyncEngine, run_id: uuid.UUID, seconds_ago: int) -> None:
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "UPDATE runs SET heartbeat_at = now() - (:secs * interval '1 second') "
                "WHERE id = :id"
            ),
            {"id": run_id, "secs": seconds_ago},
        )


async def _cleanup(engine: AsyncEngine, run_ids: list[uuid.UUID], agent_id: uuid.UUID) -> None:
    async with engine.connect() as conn, conn.begin():
        for run_id in run_ids:
            await conn.execute(text("DELETE FROM turns WHERE run_id = :id"), {"id": run_id})
            await conn.execute(
                text("DELETE FROM assertion_results WHERE run_id = :id"), {"id": run_id}
            )
            await conn.execute(text("DELETE FROM run_events WHERE run_id = :id"), {"id": run_id})
            await conn.execute(text("DELETE FROM runs WHERE id = :id"), {"id": run_id})
        await conn.execute(text("DELETE FROM agents WHERE id = :id"), {"id": agent_id})


def test_load_script_missing_file_raises() -> None:
    with pytest.raises(FileNotFoundError):
        load_script("does-not-exist.json")


def test_load_script_rejects_script_not_ending_in_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fake_runner_module, "SCRIPTS_DIR", tmp_path)
    (tmp_path / "bad.json").write_text(json.dumps([{"type": "turn", "data": {}}]))
    with pytest.raises(AssertionError, match="must end with a 'done' event"):
        load_script("bad.json")


def test_default_script_loads_and_ends_with_done() -> None:
    script = load_script("basic_simulation.json")
    assert script[-1]["type"] == "done"
    assert len(script) > 1


async def test_claim_run_claims_oldest_queued_run(engine: AsyncEngine) -> None:
    async with engine.connect() as conn, conn.begin():
        agent_id = await _seed_agent(conn)
        older_id = await _seed_queued_run(conn, agent_id)
        # Ensure a distinct, earlier created_at for the "older" run.
        await conn.execute(
            text("UPDATE runs SET created_at = now() - interval '1 minute' WHERE id = :id"),
            {"id": older_id},
        )
        newer_id = await _seed_queued_run(conn, agent_id)

    try:
        claimed = await claim_run(engine, "test-worker-1")
        assert claimed is not None
        assert claimed.id == older_id

        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT status, claimed_by, heartbeat_at FROM runs WHERE id = :id"),
                {"id": older_id},
            )
            row = result.mappings().one()
            assert row["status"] == "claimed"
            assert row["claimed_by"] == "test-worker-1"
            assert row["heartbeat_at"] is not None

        # The newer run is untouched — still queued.
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT status FROM runs WHERE id = :id"), {"id": newer_id}
            )
            assert result.scalar_one() == "queued"
    finally:
        await _cleanup(engine, [older_id, newer_id], agent_id)


async def test_concurrent_claims_never_double_claim(engine: AsyncEngine) -> None:
    """Exactly one of two concurrent claim attempts on a single queued run
    succeeds. Note: with only one candidate row and no artificial overlap,
    this can be satisfied by the `status='queued'` filter alone (whichever
    transaction commits first excludes the row for the other) rather than
    by FOR UPDATE SKIP LOCKED specifically — but "never double-claim" is
    the actual invariant the ticket cares about, and it holds either way.
    """
    async with engine.connect() as conn, conn.begin():
        agent_id = await _seed_agent(conn)
        run_id = await _seed_queued_run(conn, agent_id)

    try:
        results = await asyncio.gather(
            claim_run(engine, "worker-a"),
            claim_run(engine, "worker-b"),
        )
        successes = [r for r in results if r is not None]
        assert len(successes) == 1
        assert successes[0].id == run_id
    finally:
        await _cleanup(engine, [run_id], agent_id)


async def test_fake_runner_replays_script_and_completes_run(engine: AsyncEngine) -> None:
    async with engine.connect() as conn, conn.begin():
        agent_id = await _seed_agent(conn)
        run_id = await _seed_queued_run(conn, agent_id)

    try:
        claimed = await claim_run(engine, "test-worker-fake")
        assert claimed is not None

        await run_fake_script(engine, claimed)

        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT status, started_at, ended_at, metrics FROM runs WHERE id = :id"),
                {"id": run_id},
            )
            row = result.mappings().one()
            assert row["status"] == "completed"
            assert row["started_at"] is not None
            assert row["ended_at"] is not None
            assert row["metrics"]["resultBadge"] == "pass"

            events_result = await conn.execute(
                text("SELECT seq, type FROM run_events WHERE run_id = :id ORDER BY seq"),
                {"id": run_id},
            )
            rows = events_result.all()
            # status(running) + 8 scripted events
            assert [r.type for r in rows] == [
                "status",
                "turn",
                "turn",
                "turn",
                "assertion",
                "turn",
                "assertion",
                "metrics",
                "done",
            ]
            assert [r.seq for r in rows] == list(range(1, len(rows) + 1))
    finally:
        await _cleanup(engine, [run_id], agent_id)


async def test_execute_run_dispatches_non_simulation_types_to_fake_runner(
    engine: AsyncEngine,
) -> None:
    async with engine.connect() as conn, conn.begin():
        agent_id = await _seed_agent(conn)
        run_id = await _seed_queued_run(conn, agent_id, run_type="redteam")

    try:
        claimed = await claim_run(engine, "test-worker-redteam")
        assert claimed is not None
        assert claimed.type == "redteam"

        await execute_run(engine, claimed)

        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT status FROM runs WHERE id = :id"), {"id": run_id}
            )
            assert result.scalar_one() == "completed"
    finally:
        await _cleanup(engine, [run_id], agent_id)


async def test_update_heartbeat_advances_timestamp(engine: AsyncEngine) -> None:
    async with engine.connect() as conn, conn.begin():
        agent_id = await _seed_agent(conn)
        run_id = await _seed_queued_run(conn, agent_id)

    try:
        claimed = await claim_run(engine, "test-worker-heartbeat")
        assert claimed is not None

        async with engine.connect() as conn:
            before = (
                await conn.execute(
                    text("SELECT heartbeat_at FROM runs WHERE id = :id"), {"id": run_id}
                )
            ).scalar_one()

        await asyncio.sleep(0.01)
        await update_heartbeat(engine, run_id)

        async with engine.connect() as conn:
            after = (
                await conn.execute(
                    text("SELECT heartbeat_at FROM runs WHERE id = :id"), {"id": run_id}
                )
            ).scalar_one()

        assert after > before
    finally:
        await _cleanup(engine, [run_id], agent_id)


async def test_reap_stale_runs_marks_stale_claimed_run_failed(engine: AsyncEngine) -> None:
    async with engine.connect() as conn, conn.begin():
        agent_id = await _seed_agent(conn)
        run_id = await _seed_queued_run(conn, agent_id)

    try:
        claimed = await claim_run(engine, "test-worker-stale")
        assert claimed is not None
        await _backdate_heartbeat(engine, run_id, seconds_ago=61)

        reaped = await reap_stale_runs(engine, threshold_seconds=60)
        assert reaped == [run_id]

        async with engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT status, ended_at FROM runs WHERE id = :id"), {"id": run_id}
                    )
                )
                .mappings()
                .one()
            )
            assert row["status"] == "failed"
            assert row["ended_at"] is not None

            events = (
                (
                    await conn.execute(
                        text("SELECT type, data FROM run_events WHERE run_id = :id"), {"id": run_id}
                    )
                )
                .mappings()
                .all()
            )
            assert len(events) == 1
            assert events[0]["type"] == "error"
            assert events[0]["data"]["code"] == "worker_lost"
            assert events[0]["data"]["fatal"] is True
    finally:
        await _cleanup(engine, [run_id], agent_id)


async def test_reap_stale_runs_ignores_fresh_heartbeat(engine: AsyncEngine) -> None:
    async with engine.connect() as conn, conn.begin():
        agent_id = await _seed_agent(conn)
        run_id = await _seed_queued_run(conn, agent_id)

    try:
        claimed = await claim_run(engine, "test-worker-fresh")
        assert claimed is not None

        reaped = await reap_stale_runs(engine, threshold_seconds=60)
        assert run_id not in reaped

        async with engine.connect() as conn:
            status = (
                await conn.execute(text("SELECT status FROM runs WHERE id = :id"), {"id": run_id})
            ).scalar_one()
            assert status == "claimed"
    finally:
        await _cleanup(engine, [run_id], agent_id)


async def test_reap_stale_runs_ignores_queued_runs(engine: AsyncEngine) -> None:
    """A queued (never-claimed) run has no heartbeat to go stale — reaping
    only ever applies to claimed/running runs, per spine §5."""
    async with engine.connect() as conn, conn.begin():
        agent_id = await _seed_agent(conn)
        run_id = await _seed_queued_run(conn, agent_id)
        await conn.execute(
            text("UPDATE runs SET created_at = now() - interval '1 hour' WHERE id = :id"),
            {"id": run_id},
        )

    try:
        reaped = await reap_stale_runs(engine, threshold_seconds=60)
        assert run_id not in reaped

        async with engine.connect() as conn:
            status = (
                await conn.execute(text("SELECT status FROM runs WHERE id = :id"), {"id": run_id})
            ).scalar_one()
            assert status == "queued"
    finally:
        await _cleanup(engine, [run_id], agent_id)


async def test_concurrent_reap_never_double_reaps(engine: AsyncEngine) -> None:
    """Two concurrent reap sweeps over the same stale run produce exactly
    one worker_lost event — the same "never double-process" invariant as
    claim_run's double-claim test, and for the same reason: every worker
    process runs its own reaper tick, so overlap is a real deployment
    scenario, not a hypothetical.
    """
    async with engine.connect() as conn, conn.begin():
        agent_id = await _seed_agent(conn)
        run_id = await _seed_queued_run(conn, agent_id)

    try:
        claimed = await claim_run(engine, "test-worker-concurrent-reap")
        assert claimed is not None
        await _backdate_heartbeat(engine, run_id, seconds_ago=61)

        results = await asyncio.gather(
            reap_stale_runs(engine, threshold_seconds=60),
            reap_stale_runs(engine, threshold_seconds=60),
        )
        combined = [run for batch in results for run in batch]
        assert combined == [run_id]

        async with engine.connect() as conn:
            events = (
                await conn.execute(
                    text("SELECT type FROM run_events WHERE run_id = :id AND type = 'error'"),
                    {"id": run_id},
                )
            ).all()
            assert len(events) == 1
    finally:
        await _cleanup(engine, [run_id], agent_id)


async def test_reaper_loop_reaps_within_a_few_ticks(engine: AsyncEngine) -> None:
    async with engine.connect() as conn, conn.begin():
        agent_id = await _seed_agent(conn)
        run_id = await _seed_queued_run(conn, agent_id)

    task = None
    try:
        claimed = await claim_run(engine, "test-worker-loop")
        assert claimed is not None
        await _backdate_heartbeat(engine, run_id, seconds_ago=61)

        task = asyncio.create_task(reaper_loop(engine, interval_seconds=0.5))

        # Poll rather than a single fixed sleep: each tick's DB round-trip
        # is real network latency to Neon, which varies (a fixed 1.5s wait
        # once flaked here purely on that variance, not on reaper logic —
        # every other reaper test, including one asserting the exact same
        # outcome via a direct call, passed). A generous poll ceiling still
        # fails fast if the loop is genuinely broken.
        status = None
        for _ in range(40):
            async with engine.connect() as conn:
                status = (
                    await conn.execute(
                        text("SELECT status FROM runs WHERE id = :id"), {"id": run_id}
                    )
                ).scalar_one()
            if status == "failed":
                break
            await asyncio.sleep(0.5)
        assert status == "failed"
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await _cleanup(engine, [run_id], agent_id)


async def test_fake_runner_skips_completion_if_already_reaped(engine: AsyncEngine) -> None:
    """The resurrection-race guard: if the run is marked 'failed' (as a
    reaper would do) while FakeRunner is mid-script, the terminal `done`
    event must never be emitted and the status must not flip back to
    'completed' — otherwise run_events would hold both worker_lost and
    done, and the runs row would contradict its own event log.
    """
    async with engine.connect() as conn, conn.begin():
        agent_id = await _seed_agent(conn)
        run_id = await _seed_queued_run(conn, agent_id)

    try:
        claimed = await claim_run(engine, "test-worker-resurrection")
        assert claimed is not None

        run_task = asyncio.create_task(run_fake_script(engine, claimed))
        # Let a couple of scripted events land, then simulate an
        # out-of-band reap while the run is still mid-script.
        await asyncio.sleep(1.0)
        async with engine.connect() as conn, conn.begin():
            await conn.execute(
                text("UPDATE runs SET status = 'failed', ended_at = now() WHERE id = :id"),
                {"id": run_id},
            )
        await run_task

        async with engine.connect() as conn:
            status = (
                await conn.execute(text("SELECT status FROM runs WHERE id = :id"), {"id": run_id})
            ).scalar_one()
            assert status == "failed"

            event_types = (
                (
                    await conn.execute(
                        text("SELECT type FROM run_events WHERE run_id = :id"), {"id": run_id}
                    )
                )
                .scalars()
                .all()
            )
            assert "done" not in event_types
    finally:
        await _cleanup(engine, [run_id], agent_id)
