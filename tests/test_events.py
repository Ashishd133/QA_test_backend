"""Unit tests for typed event constructors (no DB needed) plus an
integration test proving emit()'s seq is gapless/duplicate-free under real
concurrency across separate DB sessions (B0-06's acceptance criterion).
"""

import asyncio
import uuid

from sqlalchemy import text

from app.events import assertion_event, emit, error_event, exposure_event, turn_event
from tests.conftest import _test_engine, requires_test_db


def test_turn_event_serializes_camel_case() -> None:
    event = turn_event(
        index=8, role="agent", text="hello", latency_ms=1240, flagged=True, flag_reason="slow"
    )
    assert event.type == "turn"
    assert event.data.model_dump(mode="json", by_alias=True) == {
        "index": 8,
        "role": "agent",
        "text": "hello",
        "latencyMs": 1240,
        "flagged": True,
        "flagReason": "slow",
    }


def test_assertion_event_serializes_camel_case() -> None:
    event = assertion_event(
        assertion_id="a3", name="States card-block timeline", status="passed", triggered_at_turn=8
    )
    assert event.data.model_dump(mode="json", by_alias=True) == {
        "assertionId": "a3",
        "name": "States card-block timeline",
        "status": "passed",
        "triggeredAtTurn": 8,
    }


def test_error_event_defaults_not_fatal() -> None:
    event = error_event(code="worker_lost", message="heartbeat stale")
    assert event.data.model_dump(by_alias=True)["fatal"] is False


def test_exposure_event_carries_counts_snapshot() -> None:
    event = exposure_event(counts={"pii_leak": 1, "auth_bypass": 0})
    assert event.data.model_dump(by_alias=True) == {"counts": {"pii_leak": 1, "auth_bypass": 0}}


@requires_test_db
async def test_emit_seq_is_gapless_under_concurrency() -> None:
    engine = _test_engine()
    agent_id = uuid.uuid4()
    run_id = uuid.uuid4()
    n = 20

    try:
        async with engine.connect() as conn, conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO agents (id, name, transport, created_by_user_id) "
                    "VALUES (:id, 'Concurrency Test Agent', 'web', 'user-1')"
                ),
                {"id": agent_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO runs (id, type, agent_id, created_by_user_id) "
                    "VALUES (:id, 'simulation', :agent_id, 'user-1')"
                ),
                {"id": run_id, "agent_id": agent_id},
            )

        async def emit_one(i: int) -> int:
            async with engine.connect() as conn, conn.begin():
                return await emit(conn, run_id, turn_event(index=i, role="agent", text=f"turn {i}"))

        seqs = await asyncio.gather(*(emit_one(i) for i in range(n)))
        assert sorted(seqs) == list(range(1, n + 1))

        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT seq FROM run_events WHERE run_id = :run_id ORDER BY seq"),
                {"run_id": run_id},
            )
            db_seqs = [row[0] for row in result]
        assert db_seqs == list(range(1, n + 1))
    finally:
        async with engine.connect() as conn, conn.begin():
            await conn.execute(
                text("DELETE FROM run_events WHERE run_id = :run_id"), {"run_id": run_id}
            )
            await conn.execute(text("DELETE FROM runs WHERE id = :run_id"), {"run_id": run_id})
            await conn.execute(text("DELETE FROM agents WHERE id = :id"), {"id": agent_id})
        await engine.dispose()
