from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncEngine

from app.workers.claim import ClaimedRun
from app.workers.fake_runner import run_fake_script

Executor = Callable[[AsyncEngine, ClaimedRun], Awaitable[None]]

# All run types map to FakeRunner until their real executors land
# (sim: B2, redteam: B4, discovery: B5, suite: post-MVP fan-out).
EXECUTORS: dict[str, Executor] = {
    "simulation": run_fake_script,
    "discovery": run_fake_script,
    "redteam": run_fake_script,
    "suite": run_fake_script,
}


async def execute_run(engine: AsyncEngine, claimed: ClaimedRun) -> None:
    executor = EXECUTORS.get(claimed.type, run_fake_script)
    await executor(engine, claimed)
