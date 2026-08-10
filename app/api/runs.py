import asyncio
import json
import time
import uuid
from collections.abc import AsyncGenerator
from datetime import date, datetime

import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import bindparam, text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import get_engine
from app.deps import require_user_id
from app.errors import APIError
from app.formatting import format_duration
from app.gcp_auth import signed_recording_url
from app.schemas.runs import (
    DashboardRunRow,
    DiscoveryRunCreate,
    DummyIdentity,
    RedteamRunCreate,
    ResultAssertion,
    RunCost,
    RunCreateResponse,
    RunDetail,
    SimulationRunCreate,
    TranscriptTurn,
)
from app.verdict import format_score, verdict_for_run

router = APIRouter(tags=["runs"])

_CONCURRENCY_RETRY_AFTER_MS = 5000
_LIVE_STATUSES = ("queued", "claimed", "running")

_LIVE_RUN_COUNT_SQL = text(
    "SELECT count(*) FROM runs WHERE agent_id = :agent_id AND status IN :statuses"
).bindparams(bindparam("statuses", expanding=True))

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


def _run_cost(raw: dict[str, object] | None) -> RunCost | None:
    """B2-06: `runs.cost` is null until B2-08's executor writes it; its
    JSONB shape is UsageTracker.as_dict() (app/usage.py), whose keys are
    already the camelCase RunCost aliases, so model_validate reads it as-is.
    """
    return RunCost.model_validate(raw) if raw else None


_LIST_RUNS_SQL = text(
    "SELECT r.id, r.status, r.metrics, r.created_at, r.started_at, r.ended_at, "
    "       r.project_id, r.end_reason, r.cost, "
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
        project_id=str(row["project_id"]) if row["project_id"] is not None else None,
        end_reason=row["end_reason"],
        cost=_run_cost(row["cost"]),
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
    "       r.project_id, r.end_reason, r.cost, r.recording_url, "
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
            # B2-08: prefer the judge's own rationale (assertion_event's
            # `note`) over the generic turn-trigger phrase -- FakeRunner's
            # scripted assertion events never carry one, so this falls back
            # to the old behavior for those.
            note = data.get("note")
            if note:
                detail = note
            else:
                triggered_at_turn = data.get("triggeredAtTurn")
                detail = (
                    f"Triggered at turn {triggered_at_turn}"
                    if triggered_at_turn is not None
                    else ""
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
        project_id=str(row["project_id"]) if row["project_id"] is not None else None,
        end_reason=row["end_reason"],
        cost=_run_cost(row["cost"]),
        recording_url=(
            signed_recording_url(row["recording_url"]) if row["recording_url"] else None
        ),
        created_at=_isoformat(row["created_at"]),
        transcript=transcript,
        result_assertions=result_assertions,
        latency_series=latency_series,
    )


def _isoformat(dt: datetime) -> str:
    return dt.isoformat()


def _validate_dummy_identity(identity: DummyIdentity) -> None:
    """Format only (spine §6): a valid-format-but-wrong identity is not an
    error here -- only shape failures 422 with the ticket's named
    `invalid_identity` code, not Pydantic's generic `validation_error`."""
    errors: list[dict[str, object]] = []
    if not identity.name.strip():
        errors.append({"loc": ["dummyIdentity", "name"], "msg": "must not be empty"})
    try:
        date.fromisoformat(identity.dob)
    except ValueError:
        errors.append({"loc": ["dummyIdentity", "dob"], "msg": "must be an ISO date (YYYY-MM-DD)"})
    if not identity.account.strip():
        errors.append({"loc": ["dummyIdentity", "account"], "msg": "must not be empty"})
    if not identity.verification_phrase.strip():
        errors.append({"loc": ["dummyIdentity", "verificationPhrase"], "msg": "must not be empty"})
    if errors:
        raise APIError(
            "invalid_identity",
            "dummyIdentity failed format validation",
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            details=errors,
        )


_INSERT_RUN_SQL = text(
    "INSERT INTO runs (id, type, agent_id, scenario_id, config, idempotency_key, "
    " created_by_user_id) "
    "VALUES (:id, :type, :agent_id, :scenario_id, CAST(:config AS jsonb), "
    " :idempotency_key, :user_id) "
    "ON CONFLICT (idempotency_key) DO NOTHING "
    "RETURNING id"
)


async def _create_run(
    engine: AsyncEngine,
    *,
    run_type: str,
    agent_id: uuid.UUID,
    scenario_id: uuid.UUID | None,
    config: dict[str, object],
    idempotency_key: str | None,
    user_id: str,
) -> uuid.UUID:
    """Concurrency pre-check + idempotent insert in one transaction (spine
    §5). This is a fail-fast advisory check, not the authoritative gate --
    that's claim-time enforcement, B3-03, not yet built. `idempotency_key`
    being NULL never conflicts (Postgres unique constraints never consider
    NULLs equal), so unkeyed requests always insert a fresh row."""
    async with engine.connect() as conn, conn.begin():
        agent_row = (
            (
                await conn.execute(
                    text("SELECT max_concurrency FROM agents WHERE id = :id"), {"id": agent_id}
                )
            )
            .mappings()
            .first()
        )
        if agent_row is None:
            raise APIError("not_found", "agent not found", status.HTTP_404_NOT_FOUND)

        live_count = (
            await conn.execute(
                _LIVE_RUN_COUNT_SQL,
                {"agent_id": agent_id, "statuses": list(_LIVE_STATUSES)},
            )
        ).scalar_one()
        if live_count >= agent_row["max_concurrency"]:
            raise APIError(
                "concurrency_limit",
                "agent has reached its maximum concurrent runs",
                status.HTTP_409_CONFLICT,
                details={"retryAfterMs": _CONCURRENCY_RETRY_AFTER_MS},
            )

        inserted = (
            (
                await conn.execute(
                    _INSERT_RUN_SQL,
                    {
                        "id": uuid.uuid4(),
                        "type": run_type,
                        "agent_id": agent_id,
                        "scenario_id": scenario_id,
                        "config": json.dumps(config),
                        "idempotency_key": idempotency_key,
                        "user_id": user_id,
                    },
                )
            )
            .mappings()
            .first()
        )
        if inserted is not None:
            return uuid.UUID(str(inserted["id"]))

        existing = (
            (
                await conn.execute(
                    text("SELECT id FROM runs WHERE idempotency_key = :key"),
                    {"key": idempotency_key},
                )
            )
            .mappings()
            .one()
        )
        return uuid.UUID(str(existing["id"]))


@router.post(
    "/v1/simulations/runs", response_model=RunCreateResponse, status_code=status.HTTP_202_ACCEPTED
)
async def create_simulation_run(
    body: SimulationRunCreate,
    user_id: str = Depends(require_user_id),
    engine: AsyncEngine = Depends(get_engine),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> RunCreateResponse:
    scenario_id = uuid.UUID(body.scenario_id)
    async with engine.connect() as conn:
        scenario_row = (
            (
                await conn.execute(
                    text(
                        "SELECT s.agent_id FROM scenarios sc "
                        "JOIN suites s ON s.id = sc.suite_id WHERE sc.id = :id"
                    ),
                    {"id": scenario_id},
                )
            )
            .mappings()
            .first()
        )
    if scenario_row is None:
        raise APIError("not_found", "scenario not found", status.HTTP_404_NOT_FOUND)

    run_id = await _create_run(
        engine,
        run_type="simulation",
        agent_id=scenario_row["agent_id"],
        scenario_id=scenario_id,
        config={},
        idempotency_key=idempotency_key,
        user_id=user_id,
    )
    return RunCreateResponse(run_id=str(run_id))


@router.post(
    "/v1/discovery/runs", response_model=RunCreateResponse, status_code=status.HTTP_202_ACCEPTED
)
async def create_discovery_run(
    body: DiscoveryRunCreate,
    user_id: str = Depends(require_user_id),
    engine: AsyncEngine = Depends(get_engine),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> RunCreateResponse:
    agent_id = uuid.UUID(body.agent_id)
    _validate_dummy_identity(body.dummy_identity)

    run_id = await _create_run(
        engine,
        run_type="discovery",
        agent_id=agent_id,
        scenario_id=None,
        config={"dummyIdentity": body.dummy_identity.model_dump(mode="json", by_alias=True)},
        idempotency_key=idempotency_key,
        user_id=user_id,
    )
    return RunCreateResponse(run_id=str(run_id))


@router.post(
    "/v1/redteam/runs", response_model=RunCreateResponse, status_code=status.HTTP_202_ACCEPTED
)
async def create_redteam_run(
    body: RedteamRunCreate,
    user_id: str = Depends(require_user_id),
    engine: AsyncEngine = Depends(get_engine),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> RunCreateResponse:
    agent_id = uuid.UUID(body.agent_id)

    run_id = await _create_run(
        engine,
        run_type="redteam",
        agent_id=agent_id,
        scenario_id=None,
        config={"categories": body.categories},
        idempotency_key=idempotency_key,
        user_id=user_id,
    )
    return RunCreateResponse(run_id=str(run_id))


_CANCELLABLE_STATUSES = ("queued", "claimed", "running")


@router.post("/v1/runs/{run_id}/cancel", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_run(
    run_id: uuid.UUID,
    user_id: str = Depends(require_user_id),
    engine: AsyncEngine = Depends(get_engine),
) -> None:
    """Sets status='cancelled' + NOTIFY only (spine §5) -- the actual partial
    `done` event is emitted later by whichever executor is running this run
    (app.workers.fake_runner), which checks this flag every turn. A queued
    run that never gets claimed just stays 'cancelled' with an empty event
    log, which B1-03's detail reduction already handles gracefully."""
    async with engine.connect() as conn, conn.begin():
        row = (
            (
                await conn.execute(
                    text("SELECT status FROM runs WHERE id = :id FOR UPDATE"), {"id": run_id}
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise APIError("not_found", "run not found", status.HTTP_404_NOT_FOUND)
        if row["status"] not in _CANCELLABLE_STATUSES:
            raise APIError(
                "conflict", "run has already reached a terminal state", status.HTTP_409_CONFLICT
            )
        await conn.execute(
            text("UPDATE runs SET status = 'cancelled' WHERE id = :id"), {"id": run_id}
        )
        await conn.execute(text("SELECT pg_notify('run_events', :run_id)"), {"run_id": str(run_id)})


@router.post(
    "/v1/runs/{run_id}/rerun",
    response_model=RunCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def rerun_run(
    run_id: uuid.UUID,
    user_id: str = Depends(require_user_id),
    engine: AsyncEngine = Depends(get_engine),
) -> RunCreateResponse:
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT type, agent_id, scenario_id, config FROM runs WHERE id = :id"),
                    {"id": run_id},
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        raise APIError("not_found", "run not found", status.HTTP_404_NOT_FOUND)

    new_run_id = await _create_run(
        engine,
        run_type=row["type"],
        agent_id=row["agent_id"],
        scenario_id=row["scenario_id"],
        config=row["config"] or {},
        idempotency_key=None,
        user_id=user_id,
    )
    return RunCreateResponse(run_id=str(new_run_id))
