"""B1-07: writes turns + assertion_results from the persisted event log (not
whatever in-memory state produced them -- "from the event log" per the
ticket, and the only way a scripted executor (fake_runner.py) and a real one
(B2-08's simulation.py) can agree on what "materialized" means). Extracted
from fake_runner.py so both executors share one implementation instead of
two copies drifting apart.

Scoped to turn/assertion only: node/intent/attack have no reduce-from-events
read path yet (RunDetail only exposes transcript/resultAssertions), so
there's nothing for a materialized copy of those to agree with.
"""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

_MATERIALIZE_TURNS_SQL = text(
    "SELECT data FROM run_events WHERE run_id = :run_id AND type = 'turn' ORDER BY seq"
)
_MATERIALIZE_ASSERTIONS_SQL = text(
    "SELECT data FROM run_events WHERE run_id = :run_id AND type = 'assertion' ORDER BY seq"
)
_INSERT_TURN_SQL = text(
    "INSERT INTO turns (run_id, idx, role, text, latency_ms, flagged, flag_reason) "
    "VALUES (:run_id, :idx, :role, :text, :latency_ms, :flagged, :flag_reason)"
)
_UPSERT_ASSERTION_SQL = text(
    "INSERT INTO assertion_results "
    "(run_id, assertion_id, name, status, note, triggered_at_turn) "
    "VALUES (:run_id, :assertion_id, :name, :status, :note, :triggered_at_turn) "
    "ON CONFLICT (run_id, assertion_id) DO UPDATE SET "
    "name = EXCLUDED.name, status = EXCLUDED.status, note = EXCLUDED.note, "
    "triggered_at_turn = EXCLUDED.triggered_at_turn"
)


async def materialize_run(conn: AsyncConnection, run_id: uuid.UUID) -> None:
    """`idx` is assigned by seq order here, matching _reduce_events building
    transcript in seq order -- TurnData.index is the emitting side's own turn
    number (e.g. a script's index, or a persona call's turn count) and must
    not be trusted as the materialized row order.
    """
    turn_rows = (await conn.execute(_MATERIALIZE_TURNS_SQL, {"run_id": run_id})).scalars().all()
    for idx, data in enumerate(turn_rows):
        await conn.execute(
            _INSERT_TURN_SQL,
            {
                "run_id": run_id,
                "idx": idx,
                "role": data["role"],
                "text": data["text"],
                "latency_ms": data.get("latencyMs"),
                "flagged": data.get("flagged", False),
                "flag_reason": data.get("flagReason"),
            },
        )

    assertion_rows = (
        (await conn.execute(_MATERIALIZE_ASSERTIONS_SQL, {"run_id": run_id})).scalars().all()
    )
    for data in assertion_rows:
        await conn.execute(
            _UPSERT_ASSERTION_SQL,
            {
                "run_id": run_id,
                "assertion_id": data["assertionId"],
                "name": data["name"],
                "status": data["status"],
                "note": data.get("note"),
                "triggered_at_turn": data.get("triggeredAtTurn"),
            },
        )
