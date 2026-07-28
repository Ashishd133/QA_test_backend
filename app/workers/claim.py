import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass(frozen=True)
class ClaimedRun:
    id: uuid.UUID
    type: str
    agent_id: uuid.UUID
    scenario_id: uuid.UUID | None
    config: dict[str, object]


_CLAIM_SQL = text(
    """
    UPDATE runs
    SET status = 'claimed', claimed_by = :worker_id, heartbeat_at = now()
    WHERE id = (
        SELECT id FROM runs
        WHERE status = 'queued'
        ORDER BY created_at
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id, type, agent_id, scenario_id, config
    """
)


async def claim_run(engine: AsyncEngine, worker_id: str) -> ClaimedRun | None:
    """Atomically claims the oldest queued run, or None if none is available.

    The `FOR UPDATE SKIP LOCKED` subquery and the `UPDATE` run as one
    statement in one transaction, so the claim can never race two workers
    onto the same row (spine §5) — a concurrent claimant either sees the
    row already flipped to 'claimed' (status filter excludes it) or, if
    genuinely overlapping, skips the locked row entirely.
    """
    async with engine.connect() as conn, conn.begin():
        result = await conn.execute(_CLAIM_SQL, {"worker_id": worker_id})
        row = result.mappings().first()
        if row is None:
            return None
        return ClaimedRun(
            id=row["id"],
            type=row["type"],
            agent_id=row["agent_id"],
            scenario_id=row["scenario_id"],
            config=row["config"],
        )
