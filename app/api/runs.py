import asyncio
import json
import time
import uuid
from collections.abc import AsyncGenerator

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import get_engine

router = APIRouter(tags=["runs"])

HEARTBEAT_INTERVAL_SECONDS = 15
POLL_INTERVAL_SECONDS = 2


def _format_sse(seq: int, event_type: str, data: dict[str, object]) -> str:
    return f"id: {seq}\nevent: {event_type}\ndata: {json.dumps(data)}\n\n"


def _is_terminal(event_type: str, data: dict[str, object]) -> bool:
    if event_type == "done":
        return True
    return event_type == "error" and bool(data.get("fatal"))


async def _run_exists(engine: AsyncEngine, run_id: uuid.UUID) -> bool:
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT 1 FROM runs WHERE id = :id"), {"id": run_id})
        return result.first() is not None


async def _fetch_events_after(
    engine: AsyncEngine, run_id: uuid.UUID, after_seq: int
) -> list[dict[str, object]]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT seq, type, data FROM run_events "
                "WHERE run_id = :run_id AND seq > :after_seq ORDER BY seq"
            ),
            {"run_id": run_id, "after_seq": after_seq},
        )
        return [dict(row._mapping) for row in result.all()]


async def _connect_raw(engine: AsyncEngine) -> asyncpg.Connection:
    """Derives the raw asyncpg DSN from the engine actually passed to
    event_stream(), rather than a separate settings lookup -- LISTEN/NOTIFY
    only works within one database, so this connection must land on
    whichever DB `engine`'s own queries hit (production or, in tests, the
    isolated Neon branch overriding Depends(get_engine))."""
    url = engine.url
    dsn = f"postgresql://{url.username}:{url.password}@{url.host}:{url.port or 5432}/{url.database}"
    return await asyncpg.connect(dsn, ssl="require")


async def event_stream(
    engine: AsyncEngine, run_id: uuid.UUID, after_seq: int
) -> AsyncGenerator[str, None]:
    """Replay run_events(seq > after_seq), then follow live via LISTEN/NOTIFY
    with a poll fallback (Amendment A). Terminates after a `done` event or a
    fatal `error`.

    Client disconnect isn't polled for explicitly. Starlette's modern
    StreamingResponse path (ASGI spec >= 2.4) has no separate disconnect
    listener — it detects a dead client when `send()` raises, and cancels
    this generator's task, which runs `finally` below. Since we emit at
    least a heartbeat every HEARTBEAT_INTERVAL_SECONDS, a dropped client is
    noticed (and cleaned up) within one heartbeat even with no new events.
    """
    last_seq = after_seq
    notified = asyncio.Event()

    def _on_notify(*_args: object) -> None:
        notified.set()

    raw_conn = await _connect_raw(engine)
    await raw_conn.add_listener("run_events", _on_notify)
    try:
        last_heartbeat = time.monotonic()
        while True:
            rows = await _fetch_events_after(engine, run_id, last_seq)
            for row in rows:
                seq_value = row["seq"]
                assert isinstance(seq_value, int)
                last_seq = seq_value
                event_type = str(row["type"])
                data = row["data"]
                assert isinstance(data, dict)
                yield _format_sse(last_seq, event_type, data)
                last_heartbeat = time.monotonic()
                if _is_terminal(event_type, data):
                    return

            notified.clear()
            try:
                await asyncio.wait_for(notified.wait(), timeout=POLL_INTERVAL_SECONDS)
            except TimeoutError:
                pass

            now = time.monotonic()
            if now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                yield ": heartbeat\n\n"
                last_heartbeat = now
    finally:
        # Shielded: on client disconnect this runs while the surrounding task
        # is already being cancelled, and an unshielded close() here gets
        # cancelled mid-close, logging a spurious "Exception terminating
        # connection" trace even though cleanup fundamentally succeeds.
        await asyncio.shield(raw_conn.close())


@router.get("/v1/runs/{run_id}/stream")
async def stream_run(
    run_id: uuid.UUID,
    request: Request,
    engine: AsyncEngine = Depends(get_engine),
) -> StreamingResponse:
    if not await _run_exists(engine, run_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")

    raw_last_event_id = request.headers.get("last-event-id")
    try:
        after_seq = int(raw_last_event_id) if raw_last_event_id is not None else 0
    except ValueError:
        after_seq = 0

    return StreamingResponse(
        event_stream(engine, run_id, after_seq),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
