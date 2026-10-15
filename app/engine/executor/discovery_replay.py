"""B5-04: curated-replay fallback for discovery.

In a `demo` environment a live exploration depends on LiveKit, Vertex and
the target agent all being healthy at the worst possible moment. Instead, a
real completed discovery run can be flagged as *the* demo replay
(`scripts/flag_demo_replay.py`); starting discovery there creates a normal
new run whose executor re-emits the flagged run's recorded events at their
original cadence. Everything downstream -- the live map on the SSE stream,
materialized discovery_nodes/edges/intents, drafts via
`scenarios:generate source=discovery_run` -- works on the new run exactly as
it does for a live one, which is why this is a replay *executor* and not a
shortcut that hands back the old run's id (that would also replay instantly,
since a completed run's stream does).

The flag is `runs.config.demoReplay` -- config is already free-form jsonb, so
this needs no migration and no new API surface.
"""

from __future__ import annotations

import asyncio
import json
import uuid

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.events import Event, EventType, emit, status_event
from app.schemas.events import (
    DoneData,
    EventData,
    IntentData,
    NodeData,
    TurnData,
)
from app.workers.claim import ClaimedRun
from app.workers.heartbeat import heartbeat_loop
from app.workers.materialize import materialize_discovery, materialize_run
from app.workers.rollup import maybe_close_parent

# A recorded gap this long was a stall (model latency, a slow agent), not
# something a demo audience should sit through.
DEFAULT_MAX_GAP_SECS = 8.0

# Only the discovery vocabulary is replayed. `status` is the executor's own
# lifecycle (re-emitted fresh), and a recorded `error` means the source was
# not a clean run to begin with.
_REPLAYED: dict[str, type[EventData]] = {
    "turn": TurnData,
    "node": NodeData,
    "intent": IntentData,
    "done": DoneData,
}

_FIND_SOURCE_SQL = text(
    "SELECT r.id FROM runs r "
    "JOIN agents a ON a.id = r.agent_id "
    "WHERE r.type = 'discovery' AND r.status = 'completed' "
    "  AND r.config->>'demoReplay' = 'true' "
    "  AND r.project_id = (SELECT project_id FROM runs WHERE id = :run_id) "
    "ORDER BY (r.agent_id = :agent_id) DESC, r.created_at DESC LIMIT 1"
)
_SOURCE_EVENTS_SQL = text(
    "SELECT type, data, ts FROM run_events WHERE run_id = :id "
    "AND type IN ('turn', 'node', 'intent', 'done') ORDER BY seq"
)
_SOURCE_METRICS_SQL = text("SELECT metrics, end_reason FROM runs WHERE id = :id")


async def find_replay_source(engine: AsyncEngine, claimed: ClaimedRun) -> uuid.UUID | None:
    """The flagged run for this project -- preferring one recorded against
    the same agent, falling back to any flagged run in the project (a demo
    project typically has one curated run, not one per agent)."""
    async with engine.connect() as conn:
        found = (
            await conn.execute(
                _FIND_SOURCE_SQL, {"run_id": claimed.id, "agent_id": claimed.agent_id}
            )
        ).scalar_one_or_none()
    return uuid.UUID(str(found)) if found is not None else None


async def replay_discovery(
    engine: AsyncEngine,
    claimed: ClaimedRun,
    source_id: uuid.UUID,
    *,
    max_gap_secs: float = DEFAULT_MAX_GAP_SECS,
) -> None:
    run_id = claimed.id
    async with engine.connect() as conn:
        events = (await conn.execute(_SOURCE_EVENTS_SQL, {"id": source_id})).mappings().all()
        source = (await conn.execute(_SOURCE_METRICS_SQL, {"id": source_id})).mappings().one()

    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "UPDATE runs SET status = 'running', started_at = now(), "
                "config = config || CAST(:marker AS jsonb) WHERE id = :id"
            ),
            {"id": run_id, "marker": json.dumps({"replayOf": str(source_id)})},
        )
        await emit(conn, run_id, status_event(status="running"))

    heartbeat_task = asyncio.create_task(heartbeat_loop(engine, run_id))
    try:
        previous_ts = events[0]["ts"] if events else None
        for row in events:
            gap = (row["ts"] - previous_ts).total_seconds() if previous_ts is not None else 0.0
            previous_ts = row["ts"]
            if gap > 0 and max_gap_secs > 0:
                await asyncio.sleep(min(gap, max_gap_secs))

            event_type: str = row["type"]
            data = _REPLAYED[event_type].model_validate(row["data"])
            async with engine.connect() as conn, conn.begin():
                status = (
                    await conn.execute(
                        text("SELECT status FROM runs WHERE id = :id FOR UPDATE"), {"id": run_id}
                    )
                ).scalar_one()
                if status == "cancelled":
                    # Same contract as every executor: stop at the next
                    # event, leave the partial log as-is.
                    await emit(conn, run_id, Event(type="done", data=DoneData()))
                    await materialize_run(conn, run_id)
                    await materialize_discovery(conn, run_id)
                    await conn.execute(
                        text(
                            "UPDATE runs SET ended_at = now(), "
                            "end_reason = 'cancelled' WHERE id = :id"
                        ),
                        {"id": run_id},
                    )
                    return
                if status not in ("claimed", "running"):
                    return  # reaper already resurrected this run as failed
                await emit(conn, run_id, Event(type=_event_type(event_type), data=data))
                if event_type == "done":
                    await materialize_run(conn, run_id)
                    await materialize_discovery(conn, run_id)
                    await conn.execute(
                        text(
                            "UPDATE runs SET status = 'completed', ended_at = now(), "
                            "metrics = CAST(:metrics AS jsonb), end_reason = :end_reason "
                            "WHERE id = :id"
                        ),
                        {
                            "id": run_id,
                            "metrics": json.dumps(source["metrics"] or {}),
                            "end_reason": source["end_reason"] or "completed",
                        },
                    )
                    await maybe_close_parent(conn, run_id)
    finally:
        heartbeat_task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(heartbeat_task), timeout=5.0)
        except asyncio.CancelledError:
            pass
        except TimeoutError:
            logger.warning(f"heartbeat task for replay run {run_id} did not shut down in time")


def _event_type(raw: str) -> EventType:
    assert raw in _REPLAYED
    return raw  # type: ignore[return-value]
