import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

HEARTBEAT_INTERVAL_SECONDS = 10.0


async def update_heartbeat(engine: AsyncEngine, run_id: uuid.UUID) -> None:
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text("UPDATE runs SET heartbeat_at = now() WHERE id = :id"), {"id": run_id}
        )


async def heartbeat_loop(
    engine: AsyncEngine, run_id: uuid.UUID, interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS
) -> None:
    """Runs until the caller cancels it (executors cancel this task when
    the run finishes). A stale heartbeat_at is how the reaper (B0-09)
    notices a lost worker. The claim itself already sets heartbeat_at, so
    short FakeRunner runs are never mistaken for lost workers even if this
    loop never ticks before the run completes.
    """
    while True:
        await asyncio.sleep(interval_seconds)
        await update_heartbeat(engine, run_id)
