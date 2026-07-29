"""Typed event constructors and the sole write path to run_events.

Amendment A (spine §0): the event log is the spine, not an implementation
detail. `emit()` is the ONLY function that writes to run_events — SSE
streaming (B0-07), replay, and debugging all read this table and nothing
else writes to it.
"""

import json
import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.schemas.events import (
    AssertionData,
    AttackData,
    DoneData,
    ErrorData,
    EventData,
    ExposureData,
    IntentData,
    MetricsData,
    NodeData,
    StatusData,
    TurnData,
)

EventType = Literal[
    "status",
    "turn",
    "metrics",
    "assertion",
    "node",
    "intent",
    "attack",
    "exposure",
    "done",
    "error",
]


@dataclass(frozen=True)
class Event:
    type: EventType
    data: EventData


def status_event(
    status: Literal["queued", "claimed", "running", "completed", "cancelled", "failed"],
) -> Event:
    return Event(type="status", data=StatusData(status=status))


def turn_event(
    *,
    index: int,
    role: Literal["caller", "agent"],
    text: str,
    latency_ms: int | None = None,
    flagged: bool = False,
    flag_reason: str | None = None,
) -> Event:
    return Event(
        type="turn",
        data=TurnData(
            index=index,
            role=role,
            text=text,
            latency_ms=latency_ms,
            flagged=flagged,
            flag_reason=flag_reason,
        ),
    )


def metrics_event(
    *,
    score: float | None = None,
    avg_latency_ms: float | None = None,
    turns_completed: int | None = None,
    interruptions: int | None = None,
) -> Event:
    return Event(
        type="metrics",
        data=MetricsData(
            score=score,
            avg_latency_ms=avg_latency_ms,
            turns_completed=turns_completed,
            interruptions=interruptions,
        ),
    )


def assertion_event(
    *,
    assertion_id: str,
    name: str,
    status: Literal["passed", "failed"],
    triggered_at_turn: int | None = None,
) -> Event:
    return Event(
        type="assertion",
        data=AssertionData(
            assertion_id=assertion_id, name=name, status=status, triggered_at_turn=triggered_at_turn
        ),
    )


def node_event(
    *,
    node_id: str,
    label: str,
    x: float,
    y: float,
    state: str,
    blocked_reason: str | None = None,
) -> Event:
    return Event(
        type="node",
        data=NodeData(
            node_id=node_id, label=label, x=x, y=y, state=state, blocked_reason=blocked_reason
        ),
    )


def intent_event(*, name: str, state: str, path: str, reason: str | None = None) -> Event:
    return Event(type="intent", data=IntentData(name=name, state=state, path=path, reason=reason))


def attack_event(
    *,
    category: str,
    attack_prompt: str,
    agent_response: str,
    verdict: Literal["blocked", "leaked", "bypassed"] | None = None,
    severity: str | None = None,
) -> Event:
    return Event(
        type="attack",
        data=AttackData(
            category=category,
            attack_prompt=attack_prompt,
            agent_response=agent_response,
            verdict=verdict,
            severity=severity,
        ),
    )


def exposure_event(*, counts: dict[str, int]) -> Event:
    return Event(type="exposure", data=ExposureData(counts=counts))


def done_event(
    *,
    score: float | None = None,
    result_badge: Literal["pass", "warn", "fail"] | None = None,
    discovery_result: dict[str, object] | None = None,
    findings_count: int | None = None,
    exposure: dict[str, int] | None = None,
) -> Event:
    return Event(
        type="done",
        data=DoneData(
            score=score,
            result_badge=result_badge,
            discovery_result=discovery_result,
            findings_count=findings_count,
            exposure=exposure,
        ),
    )


def error_event(*, code: str, message: str, fatal: bool = False) -> Event:
    return Event(type="error", data=ErrorData(code=code, message=message, fatal=fatal))


async def emit(conn: AsyncConnection, run_id: uuid.UUID, event: Event) -> int:
    """Append `event` to run_events and return its seq.

    Concurrent emits for the same run are serialized with a transaction-
    scoped advisory lock (auto-released at commit/rollback, so it never
    outlives the caller's transaction), then seq = MAX(seq)+1 is computed
    and inserted atomically — a gapless, duplicate-free sequence even when
    two workers race to write the same run's event log.
    """
    await conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": str(run_id)})
    result = await conn.execute(
        text(
            "INSERT INTO run_events (run_id, seq, type, data) "
            "SELECT :run_id, COALESCE(MAX(seq), 0) + 1, :type, CAST(:data AS jsonb) "
            "FROM run_events WHERE run_id = :run_id "
            "RETURNING seq"
        ),
        {
            "run_id": run_id,
            "type": event.type,
            "data": json.dumps(event.data.model_dump(mode="json", by_alias=True)),
        },
    )
    seq = result.scalar_one()
    await conn.execute(text("SELECT pg_notify('run_events', :run_id)"), {"run_id": str(run_id)})
    return int(seq)
