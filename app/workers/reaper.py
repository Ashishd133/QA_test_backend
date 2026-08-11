import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.events import emit, error_event, status_event
from app.workers.rollup import maybe_close_parent

STALE_THRESHOLD_SECONDS = 60
REAP_INTERVAL_SECONDS = 15.0

_FIND_STALE_SQL = text(
    """
    SELECT id FROM runs
    WHERE status IN ('claimed', 'running')
      AND heartbeat_at < now() - (:threshold_seconds * interval '1 second')
    FOR UPDATE SKIP LOCKED
    """
)

# B2.5-04: a parent whose children are all terminal but which is itself
# still non-terminal -- the case maybe_close_parent's own transaction
# should already have closed, but a crash between a child's status write
# and its maybe_close_parent call (or a bug in a code path that forgets to
# call it) can leave one behind. "No parent ever running with zero live
# children" is B2.5-04's named soak assertion; this is what makes it true
# even after a crash, not just on the happy path.
_FIND_ORPHANED_PARENTS_SQL = text(
    """
    SELECT p.id FROM runs p
    WHERE p.parent_run_id IS NULL
      AND p.status NOT IN ('completed', 'cancelled', 'failed')
      AND EXISTS (SELECT 1 FROM runs c WHERE c.parent_run_id = p.id)
      AND NOT EXISTS (
        SELECT 1 FROM runs c WHERE c.parent_run_id = p.id
          AND c.status NOT IN ('completed', 'cancelled', 'failed')
      )
    FOR UPDATE SKIP LOCKED
    """
)


async def reap_stale_runs(
    engine: AsyncEngine, threshold_seconds: int = STALE_THRESHOLD_SECONDS
) -> list[uuid.UUID]:
    """Marks claimed/running runs with a stale heartbeat_at as failed, with
    a worker_lost fatal error event — spine §5: no zombie 'running' runs in
    the UI, ever. Reaping and the terminal error event are bundled in one
    transaction per run (same discipline as FakeRunner's completion flip).

    FOR UPDATE SKIP LOCKED matters here specifically: the reaper runs in
    every worker process (no separate reaper deployable), so multiple
    reaper ticks can genuinely overlap in real deployments — this is what
    stops two workers from double-reaping (and double-emitting worker_lost
    for) the same stale run.
    """
    reaped: list[uuid.UUID] = []
    async with engine.connect() as conn, conn.begin():
        result = await conn.execute(_FIND_STALE_SQL, {"threshold_seconds": threshold_seconds})
        stale_ids = [row.id for row in result.all()]
        for run_id in stale_ids:
            await emit(
                conn,
                run_id,
                error_event(
                    code="worker_lost",
                    message=f"no heartbeat in over {threshold_seconds}s",
                    fatal=True,
                ),
            )
            await conn.execute(
                text(
                    "UPDATE runs SET status = 'failed', ended_at = now() "
                    "WHERE id = :id AND status IN ('claimed', 'running')"
                ),
                {"id": run_id},
            )
            await maybe_close_parent(conn, run_id)
            reaped.append(run_id)
    return reaped


async def reconcile_orphaned_parents(engine: AsyncEngine) -> list[uuid.UUID]:
    """B2.5-04: the reaper's own belt-and-suspenders check -- see
    `_FIND_ORPHANED_PARENTS_SQL`'s comment for when this fires."""
    reconciled: list[uuid.UUID] = []
    async with engine.connect() as conn, conn.begin():
        result = await conn.execute(_FIND_ORPHANED_PARENTS_SQL)
        parent_ids = [row.id for row in result.all()]
        for parent_id in parent_ids:
            await conn.execute(
                text("UPDATE runs SET status = 'completed', ended_at = now() WHERE id = :id"),
                {"id": parent_id},
            )
            await emit(conn, parent_id, status_event(status="completed"))
            reconciled.append(parent_id)
    return reconciled


async def reaper_loop(engine: AsyncEngine, interval_seconds: float = REAP_INTERVAL_SECONDS) -> None:
    """Runs until the caller cancels it — every worker process runs one of
    these for its whole lifetime, alongside its claim loop."""
    while True:
        await asyncio.sleep(interval_seconds)
        await reap_stale_runs(engine)
        await reconcile_orphaned_parents(engine)
