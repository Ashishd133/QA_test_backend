import asyncio
import contextlib
import json
from pathlib import Path
from typing import cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.events import Event, EventType, emit, status_event
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
from app.workers.claim import ClaimedRun
from app.workers.heartbeat import heartbeat_loop

SCRIPTS_DIR = Path(__file__).resolve().parent / "scripts"
DEFAULT_SCRIPT = "basic_simulation.json"

_EVENT_DATA_MODELS: dict[str, type[EventData]] = {
    "status": StatusData,
    "turn": TurnData,
    "metrics": MetricsData,
    "assertion": AssertionData,
    "node": NodeData,
    "intent": IntentData,
    "attack": AttackData,
    "exposure": ExposureData,
    "done": DoneData,
    "error": ErrorData,
}


def load_script(name: str) -> list[dict[str, object]]:
    """Scripts are resolved relative to this module, not the process cwd —
    `python -m app.workers.main` must find them regardless of where it's
    launched from (the same class of bug as the OpenAPI export needing
    `-m` instead of a bare script path)."""
    data = json.loads((SCRIPTS_DIR / name).read_text())
    assert isinstance(data, list) and data, f"script {name} must be a non-empty list"
    assert data[-1]["type"] == "done", f"script {name} must end with a 'done' event"
    return data


def _build_event(entry: dict[str, object]) -> Event:
    event_type = entry["type"]
    assert isinstance(event_type, str)
    model_cls = _EVENT_DATA_MODELS.get(event_type)
    if model_cls is None:
        raise ValueError(f"unknown event type in script: {event_type!r}")
    raw_data = entry["data"]
    assert isinstance(raw_data, dict)
    return Event(type=cast(EventType, event_type), data=model_cls.model_validate(raw_data))


async def run_fake_script(
    engine: AsyncEngine, claimed: ClaimedRun, script_name: str = DEFAULT_SCRIPT
) -> None:
    """Replays a scripted event sequence at prototype cadence (`delayMs` per
    entry) through emit() — the only executor until B2 wires the real
    engine. The runner owns lifecycle status (running -> completed); the
    script owns domain events (turn/assertion/metrics/.../done) — no status
    transitions belong in script JSON.
    """
    script = load_script(script_name)

    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text("UPDATE runs SET status = 'running', started_at = now() WHERE id = :id"),
            {"id": claimed.id},
        )
        await emit(conn, claimed.id, status_event(status="running"))

    heartbeat_task = asyncio.create_task(heartbeat_loop(engine, claimed.id))
    try:
        last_index = len(script) - 1
        for index, entry in enumerate(script):
            delay_ms = entry.get("delayMs", 0)
            assert isinstance(delay_ms, int)
            if delay_ms:
                await asyncio.sleep(delay_ms / 1000)

            event = _build_event(entry)

            async with engine.connect() as conn, conn.begin():
                if index == last_index:
                    # Reaper-resurrection guard (inverse of the crash case
                    # above): if the reaper already marked this run 'failed'
                    # + worker_lost while we were mid-script (e.g. a stalled
                    # network call in the real B2 executor — unreachable for
                    # FakeRunner's ~2.7s scripts, but this is the seam B3-04
                    # stress-tests), we must NOT emit `done` or flip back to
                    # 'completed': that would put a second, contradictory
                    # terminal frame in run_events on top of worker_lost.
                    # SELECT ... FOR UPDATE serializes against the reaper's
                    # own FOR UPDATE SKIP LOCKED on the same row.
                    current_status = (
                        await conn.execute(
                            text("SELECT status FROM runs WHERE id = :id FOR UPDATE"),
                            {"id": claimed.id},
                        )
                    ).scalar_one()
                    if current_status not in ("claimed", "running"):
                        return

                    # The full DoneData dump (including nulls for fields that
                    # don't apply to this run type) is a FakeRunner-only
                    # placeholder — B2's real simulation executor should
                    # define runs.metrics's actual shape (score/avg_latency/
                    # wer/sentiment per spine §3), not inherit this verbatim.
                    await emit(conn, claimed.id, event)
                    metrics = event.data.model_dump(mode="json", by_alias=True)
                    await conn.execute(
                        text(
                            "UPDATE runs SET status = 'completed', ended_at = now(), "
                            "metrics = CAST(:metrics AS jsonb) WHERE id = :id"
                        ),
                        {"id": claimed.id, "metrics": json.dumps(metrics)},
                    )
                else:
                    await emit(conn, claimed.id, event)
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
