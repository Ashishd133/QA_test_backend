import asyncio
import json
import time
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import get_engine
from app.errors import APIError
from app.formatting import format_duration
from app.schemas.runs import DashboardRunRow, ResultAssertion, RunDetail, TranscriptTurn
from app.verdict import format_score, verdict_for_run

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


_RUN_TITLE_BY_TYPE = {
    "discovery": "Discovery Run",
    "redteam": "Red Team Run",
    "suite": "Suite Run",
}


def _run_title(run_type: str, scenario_name: str | None) -> str:
    return scenario_name or _RUN_TITLE_BY_TYPE.get(run_type, "Run")


_LIST_RUNS_SQL = text(
    "SELECT r.id, r.status, r.metrics, r.created_at, r.started_at, r.ended_at, "
    "       a.name AS agent_name, s.name AS suite_name "
    "FROM runs r "
    "JOIN agents a ON a.id = r.agent_id "
    "LEFT JOIN scenarios sc ON sc.id = r.scenario_id "
    "LEFT JOIN suites s ON s.id = sc.suite_id "
    "WHERE (CAST(:type AS text) IS NULL OR r.type = CAST(:type AS text)) "
    "  AND (CAST(:agent_id AS uuid) IS NULL OR r.agent_id = CAST(:agent_id AS uuid)) "
    "  AND (CAST(:status AS text) IS NULL OR r.status = CAST(:status AS text)) "
    "  AND (CAST(:suite_id AS uuid) IS NULL OR s.id = CAST(:suite_id AS uuid)) "
    "ORDER BY r.created_at DESC "
    "LIMIT :limit"
)


def _dashboard_run_row(row: RowMapping) -> DashboardRunRow:
    run_id = str(row["id"])
    metrics = row["metrics"] or {}
    return DashboardRunRow(
        id=run_id,
        suite=row["suite_name"] or "",
        agent=row["agent_name"],
        status=verdict_for_run(row["status"], row["metrics"]),
        pass_rate=format_score(metrics.get("score")),
        duration=format_duration(row["started_at"], row["ended_at"]),
        run_id=run_id,
    )


@router.get("/v1/runs", response_model=list[DashboardRunRow])
async def list_runs(
    engine: AsyncEngine = Depends(get_engine),
    type: str | None = Query(default=None),  # noqa: A002
    suite_id: uuid.UUID | None = Query(default=None, alias="suiteId"),
    agent_id: uuid.UUID | None = Query(default=None, alias="agentId"),
    status: str | None = Query(default=None),  # noqa: A002
    limit: int = Query(default=50, ge=1, le=500),
) -> list[DashboardRunRow]:
    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    _LIST_RUNS_SQL,
                    {
                        "type": type,
                        "agent_id": agent_id,
                        "status": status,
                        "suite_id": suite_id,
                        "limit": limit,
                    },
                )
            )
            .mappings()
            .all()
        )
    return [_dashboard_run_row(row) for row in rows]


_RUN_DETAIL_SQL = text(
    "SELECT r.id, r.type, r.status, r.metrics, r.created_at, r.started_at, r.ended_at, "
    "       a.name AS agent_name, a.transport, a.language, "
    "       sc.name AS scenario_name, s.name AS suite_name "
    "FROM runs r "
    "JOIN agents a ON a.id = r.agent_id "
    "LEFT JOIN scenarios sc ON sc.id = r.scenario_id "
    "LEFT JOIN suites s ON s.id = sc.suite_id "
    "WHERE r.id = :id"
)

_RUN_EVENTS_SQL = text("SELECT type, data FROM run_events WHERE run_id = :run_id ORDER BY seq")


def _reduce_events(
    events: list[RowMapping],
) -> tuple[list[TranscriptTurn], list[ResultAssertion], list[float], dict[str, object]]:
    """Reduced live from run_events (spine: events remain the truth) rather
    than runs.metrics -- that column only ever holds the terminal `done`
    event's fields (score/resultBadge/...), not the separate `metrics`
    event's avgLatencyMs/turnsCompleted/interruptions."""
    transcript: list[TranscriptTurn] = []
    latency_series: list[float] = []
    assertions_by_id: dict[str, ResultAssertion] = {}
    latest_metrics_event: dict[str, object] = {}

    for row in events:
        data = row["data"]
        if row["type"] == "turn":
            latency_ms = data.get("latencyMs")
            transcript.append(
                TranscriptTurn(
                    role=data["role"],
                    text=data["text"],
                    lat=f"{round(latency_ms)}ms" if isinstance(latency_ms, int | float) else None,
                    flag=data.get("flagged"),
                    flag_text=data.get("flagReason"),
                )
            )
            if isinstance(latency_ms, int | float):
                latency_series.append(float(latency_ms))
        elif row["type"] == "assertion":
            triggered_at_turn = data.get("triggeredAtTurn")
            detail = (
                f"Triggered at turn {triggered_at_turn}" if triggered_at_turn is not None else ""
            )
            assertions_by_id[data["assertionId"]] = ResultAssertion(
                text=data["name"],
                detail=detail,
                ok=data["status"] == "passed",
            )
        elif row["type"] == "metrics":
            latest_metrics_event = data

    return transcript, list(assertions_by_id.values()), latency_series, latest_metrics_event


@router.get("/v1/runs/{run_id}", response_model=RunDetail)
async def get_run(run_id: uuid.UUID, engine: AsyncEngine = Depends(get_engine)) -> RunDetail:
    async with engine.connect() as conn:
        row = (await conn.execute(_RUN_DETAIL_SQL, {"id": run_id})).mappings().first()
        if row is None:
            raise APIError("not_found", "run not found", status.HTTP_404_NOT_FOUND)
        events = (await conn.execute(_RUN_EVENTS_SQL, {"run_id": run_id})).mappings().all()

    transcript, result_assertions, latency_series, event_metrics = _reduce_events(list(events))
    done_metrics = row["metrics"] or {}
    score = done_metrics.get("score")
    avg_latency_ms = event_metrics.get("avgLatencyMs")
    agent_meta = f"{row['transport']} · {row['language']}" if row["language"] else row["transport"]

    return RunDetail(
        id=str(row["id"]),
        title=_run_title(row["type"], row["scenario_name"]),
        suite_name=row["suite_name"] or "",
        scenario_name=row["scenario_name"] or "",
        agent_name=row["agent_name"],
        agent_meta=agent_meta,
        status=verdict_for_run(row["status"], row["metrics"]),
        score=round(score * 100) if isinstance(score, int | float) else 0,
        avg_latency=(
            f"{round(avg_latency_ms)}ms" if isinstance(avg_latency_ms, int | float) else "-"
        ),
        wer="-",
        sentiment="-",
        duration=format_duration(row["started_at"], row["ended_at"]),
        created_at=_isoformat(row["created_at"]),
        transcript=transcript,
        result_assertions=result_assertions,
        latency_series=latency_series,
    )


def _isoformat(dt: datetime) -> str:
    return dt.isoformat()
