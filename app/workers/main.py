import asyncio
import contextlib
import os
import socket

from app.db import get_engine
from app.workers.claim import claim_run
from app.workers.executors import execute_run
from app.workers.reaper import reaper_loop

POLL_INTERVAL_SECONDS = 1.0


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


async def _claim_loop(worker_id: str) -> None:
    engine = get_engine()
    while True:
        claimed = await claim_run(engine, worker_id)
        if claimed is None:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            continue
        print(f"worker {worker_id} claimed run {claimed.id} ({claimed.type})", flush=True)
        await execute_run(engine, claimed)


async def run_forever() -> None:
    engine = get_engine()
    worker_id = _worker_id()
    print(f"worker {worker_id} starting", flush=True)

    # Every worker process runs its own reaper tick — spine §5/§9 has no
    # separate reaper deployable, and FOR UPDATE SKIP LOCKED (app.workers.
    # reaper) keeps concurrent workers' reap sweeps from double-processing
    # the same stale run.
    reaper_task = asyncio.create_task(reaper_loop(engine))
    try:
        await _claim_loop(worker_id)
    finally:
        reaper_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reaper_task


def main() -> None:
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
