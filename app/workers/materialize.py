"""B1-07: writes turns + assertion_results from the persisted event log (not
whatever in-memory state produced them -- "from the event log" per the
ticket, and the only way a scripted executor (fake_runner.py) and a real one
(B2-08's simulation.py) can agree on what "materialized" means). Extracted
from fake_runner.py so both executors share one implementation instead of
two copies drifting apart.

Turn/assertion/metric_result as before. B5-03 adds discovery's node/intent
events (-> discovery_nodes/edges/intents); attack still has no
reduce-from-events read path, so nothing to materialize for it yet.
"""

import json
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

_MATERIALIZE_TURNS_SQL = text(
    "SELECT data FROM run_events WHERE run_id = :run_id AND type = 'turn' ORDER BY seq"
)
_MATERIALIZE_ASSERTIONS_SQL = text(
    "SELECT data FROM run_events WHERE run_id = :run_id AND type = 'assertion' ORDER BY seq"
)
_MATERIALIZE_METRIC_RESULTS_SQL = text(
    "SELECT data FROM run_events WHERE run_id = :run_id AND type = 'metric_result' ORDER BY seq"
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
_UPSERT_METRIC_RESULT_SQL = text(
    "INSERT INTO metric_results "
    "(run_id, metric_id, metric_version, status, value, turn_refs, rationale) "
    "VALUES (:run_id, :metric_id, :metric_version, :status, "
    " CAST(:value AS jsonb), :turn_refs, :rationale) "
    "ON CONFLICT (run_id, metric_id) DO UPDATE SET "
    "metric_version = EXCLUDED.metric_version, status = EXCLUDED.status, "
    "value = EXCLUDED.value, turn_refs = EXCLUDED.turn_refs, rationale = EXCLUDED.rationale"
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

    metric_result_rows = (
        (await conn.execute(_MATERIALIZE_METRIC_RESULTS_SQL, {"run_id": run_id})).scalars().all()
    )
    for data in metric_result_rows:
        await conn.execute(
            _UPSERT_METRIC_RESULT_SQL,
            {
                "run_id": run_id,
                "metric_id": uuid.UUID(data["metricId"]),
                "metric_version": data["metricVersion"],
                "status": data["status"],
                "value": json.dumps(data.get("value")),
                "turn_refs": data.get("turnRefs", []),
                "rationale": data.get("rationale"),
            },
        )


_MATERIALIZE_NODES_SQL = text(
    "SELECT data FROM run_events WHERE run_id = :run_id AND type = 'node' ORDER BY seq"
)
_MATERIALIZE_INTENTS_SQL = text(
    "SELECT data FROM run_events WHERE run_id = :run_id AND type = 'intent' ORDER BY seq"
)
_UPSERT_NODE_SQL = text(
    "INSERT INTO discovery_nodes (run_id, node_id, label, x, y, state, blocked_reason) "
    "VALUES (:run_id, :node_id, :label, :x, :y, :state, :blocked_reason) "
    "ON CONFLICT (run_id, node_id) DO UPDATE SET label = EXCLUDED.label, x = EXCLUDED.x, "
    "y = EXCLUDED.y, state = EXCLUDED.state, blocked_reason = EXCLUDED.blocked_reason"
)
_INSERT_EDGE_SQL = text(
    "INSERT INTO discovery_edges (run_id, from_node, to_node) "
    "VALUES (:run_id, :from_node, :to_node) ON CONFLICT DO NOTHING"
)
_UPSERT_INTENT_SQL = text(
    "INSERT INTO discovery_intents (run_id, name, state, path, reason) "
    "VALUES (:run_id, :name, :state, :path, :reason) "
    "ON CONFLICT (run_id, name) DO UPDATE SET state = EXCLUDED.state, "
    "path = EXCLUDED.path, reason = EXCLUDED.reason"
)


async def materialize_discovery(conn: AsyncConnection, run_id: uuid.UUID) -> None:
    """B5-03: node and intent events are upserts keyed by node_id / name, so
    the materialized rows are the *last* frame per key -- reduced from the
    event log, same invariant as `materialize_run`. Edges come from each
    node's `fromNodes`; edge rows reference nodes by FK, so nodes go first
    and an edge to an unknown node is dropped rather than failing the run."""
    nodes: dict[str, dict[str, object]] = {}
    for data in (await conn.execute(_MATERIALIZE_NODES_SQL, {"run_id": run_id})).scalars().all():
        nodes[data["nodeId"]] = data
    for node_id, data in nodes.items():
        await conn.execute(
            _UPSERT_NODE_SQL,
            {
                "run_id": run_id,
                "node_id": node_id,
                "label": data["label"],
                "x": data["x"],
                "y": data["y"],
                "state": data["state"],
                "blocked_reason": data.get("blockedReason"),
            },
        )
    for node_id, data in nodes.items():
        from_nodes = data.get("fromNodes") or []
        assert isinstance(from_nodes, list)
        for from_node in from_nodes:
            if from_node in nodes:
                await conn.execute(
                    _INSERT_EDGE_SQL,
                    {"run_id": run_id, "from_node": from_node, "to_node": node_id},
                )

    intents: dict[str, dict[str, object]] = {}
    for data in (await conn.execute(_MATERIALIZE_INTENTS_SQL, {"run_id": run_id})).scalars().all():
        intents[data["name"]] = data
    for name, data in intents.items():
        await conn.execute(
            _UPSERT_INTENT_SQL,
            {
                "run_id": run_id,
                "name": name,
                "state": data["state"],
                "path": data["path"],
                "reason": data.get("reason"),
            },
        )
