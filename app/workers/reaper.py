import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.events import emit, error_event

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
            reaped.append(run_id)
    return reaped


async def reaper_loop(engine: AsyncEngine, interval_seconds: float = REAP_INTERVAL_SECONDS) -> None:
    """Runs until the caller cancels it — every worker process runs one of
    these for its whole lifetime, alongside its claim loop."""
    while True:
        await asyncio.sleep(interval_seconds)
        await reap_stale_runs(engine)
