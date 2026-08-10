from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncEngine

from app.engine.executor.simulation import run_simulation
from app.workers.claim import ClaimedRun
from app.workers.fake_runner import run_fake_script

Executor = Callable[[AsyncEngine, ClaimedRun], Awaitable[None]]

# B2-08: simulation runs get the real executor now. redteam/discovery/suite
# still map to FakeRunner until their own real executors land (redteam: B4,
# discovery: B5, suite: post-MVP fan-out).
EXECUTORS: dict[str, Executor] = {
    "simulation": run_simulation,
    "discovery": run_fake_script,
    "redteam": run_fake_script,
    "suite": run_fake_script,
}


async def execute_run(engine: AsyncEngine, claimed: ClaimedRun) -> None:
    executor = EXECUTORS.get(claimed.type, run_fake_script)
    await executor(engine, claimed)
