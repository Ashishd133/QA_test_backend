import asyncio
import hashlib
import json
import time
import uuid
from collections import Counter
from collections.abc import AsyncGenerator
from datetime import date, datetime
from typing import Any, Literal

import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import bindparam, text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.db import get_engine
from app.deps import ensure_project_match, require_project_id, require_user_id
from app.errors import APIError
from app.formatting import format_duration
from app.gcp_auth import signed_recording_url
from app.schemas.runs import (
    DashboardRunRow,
    DiscoveryRunCreate,
    DummyIdentity,
    RedteamRunCreate,
    ResultAssertion,
    RunAggregate,
    RunCost,
    RunCreateResponse,
    RunDeltaRow,
    RunDetail,
    RunsDelta,
    SimulationRunCreate,
    TranscriptTurn,
)
from app.verdict import format_score, verdict_for_run
from app.workers.rollup import maybe_close_parent

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


async def _run_visible(
    engine: AsyncEngine, run_id: uuid.UUID, project_id: uuid.UUID | None
) -> bool:
    """`project_id=None` means the caller sent no X-Project-Id (the SSE
    exemption -- see stream_run) and only existence is checked. When it IS
    present, this enforces the match: a browser EventSource can't set
    custom headers, but a BFF proxying this endpoint can attach one, so a
    header that does arrive is honored rather than ignored."""
    async with engine.connect() as conn:
        if project_id is None:
            result = await conn.execute(text("SELECT 1 FROM runs WHERE id = :id"), {"id": run_id})
        else:
            result = await conn.execute(
                text("SELECT 1 FROM runs WHERE id = :id AND project_id = :project_id"),
                {"id": run_id, "project_id": project_id},
            )
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
    # B2.5-01: X-Project-Id is NOT required here (see app.deps's exemption
    # list docstring -- browser EventSource cannot set custom headers), but
    # IS enforced when present, e.g. from a BFF that proxies this request
    # and can attach it server-side.
    raw_project_id = getattr(request.state, "project_id", None)
    project_id = uuid.UUID(str(raw_project_id)) if raw_project_id else None
    if not await _run_visible(engine, run_id, project_id):
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
    "       r.project_id, r.end_reason, r.cost, r.parent_run_id, "
    "       a.name AS agent_name, s.name AS suite_name "
    "FROM runs r "
    "JOIN agents a ON a.id = r.agent_id "
    "LEFT JOIN scenarios sc ON sc.id = r.scenario_id "
    "LEFT JOIN suites s ON s.id = sc.suite_id "
    "WHERE r.project_id = :project_id "
    "  AND (CAST(:type AS text) IS NULL OR r.type = CAST(:type AS text)) "
    "  AND (CAST(:agent_id AS uuid) IS NULL OR r.agent_id = CAST(:agent_id AS uuid)) "
    "  AND (CAST(:status AS text) IS NULL OR r.status = CAST(:status AS text)) "
    "  AND (CAST(:suite_id AS uuid) IS NULL OR s.id = CAST(:suite_id AS uuid)) "
    # B2.5-03: `isParent=true` -> only Test Runs (parent_run_id IS NULL);
    # `parentRunId=<id>` -> only that Test Run's Calls. Undefined (NULL)
    # params are no-ops, same CAST(... AS x) IS NULL pattern as the filters
    # above -- one flag ever governs one thing, so both can be combined
    # (e.g. isParent=false with no parentRunId lists every Call system-wide,
    # which is deliberately allowed rather than special-cased away).
    "  AND (CAST(:is_parent AS boolean) IS NULL "
    "       OR (CAST(:is_parent AS boolean) = true) = (r.parent_run_id IS NULL)) "
    "  AND (CAST(:parent_run_id AS uuid) IS NULL "
    "       OR r.parent_run_id = CAST(:parent_run_id AS uuid)) "
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
        project_id=str(row["project_id"]),
        end_reason=row["end_reason"],
        cost=_run_cost(row["cost"]),
        is_parent=row["parent_run_id"] is None,
        parent_run_id=str(row["parent_run_id"]) if row["parent_run_id"] is not None else None,
    )


# B2.5-05: hard cap on the delta endpoint's row count -- documented here
# per the ticket's explicit instruction, not just in a comment nobody reads.
_DELTA_ROW_CAP = 200

_DELTA_STATE_SQL = text(
    "SELECT count(*) AS n, max(updated_at) AS max_updated_at "
    "FROM runs WHERE parent_run_id = :parent_run_id AND project_id = :project_id"
)

_DELTA_ROWS_SQL = text(
    "SELECT r.id, r.status, r.metrics, r.started_at, r.ended_at, "
    "       (SELECT count(*) FROM turns t WHERE t.run_id = r.id) AS turn_count, "
    "       (SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY t.latency_ms) "
    "        FROM turns t WHERE t.run_id = r.id AND t.latency_ms IS NOT NULL) AS latency_p95 "
    "FROM runs r "
    "WHERE r.parent_run_id = :parent_run_id AND r.project_id = :project_id "
    "  AND (CAST(:since AS timestamptz) IS NULL "
    "       OR r.updated_at > CAST(:since AS timestamptz)) "
    "ORDER BY r.updated_at DESC "
    "LIMIT :cap"
)


def _delta_etag(parent_run_id: uuid.UUID, count: int, max_updated_at: datetime | None) -> str:
    """Weak ETag over (parent, child count, latest child update) -- cheap
    to compute (one indexed aggregate query, migration 007's trigger is
    what makes `updated_at` trustworthy), and changes iff anything about
    the batch changed, independent of the caller's own `since` cursor."""
    basis = f"{parent_run_id}:{count}:{max_updated_at.isoformat() if max_updated_at else 'none'}"
    return 'W/"' + hashlib.sha256(basis.encode()).hexdigest()[:16] + '"'


def _run_delta_row(row: RowMapping) -> RunDeltaRow:
    metrics = row["metrics"] or {}
    score = metrics.get("score")
    duration_ms = None
    if row["started_at"] is not None and row["ended_at"] is not None:
        duration_ms = round((row["ended_at"] - row["started_at"]).total_seconds() * 1000)
    return RunDeltaRow(
        id=str(row["id"]),
        status=row["status"],
        score=score if isinstance(score, int | float) else None,
        turns=row["turn_count"],
        duration_ms=duration_ms,
        latency_p95=row["latency_p95"],
        goal_met=None,
    )


async def _run_delta(
    conn: AsyncConnection,
    response: Response,
    *,
    project_id: uuid.UUID,
    parent_run_id: uuid.UUID | None,
    since: datetime | None,
    if_none_match: str | None,
) -> RunsDelta | Response:
    """B2.5-05: the endpoint docstring's rule, enforced here -- this is the
    ONLY per-call polling path. A single call's own live drawer opens its
    own SSE stream (`stream_run`); nothing here opens one stream per row.

    Module-level (not nested in `list_runs`) on purpose: `list_runs`
    declares a `status` *query param*, which shadows the `fastapi.status`
    module for the rest of that function's body -- every `status.HTTP_*`
    reference here would resolve to a `str | None` inside it instead.
    """
    if parent_run_id is None:
        raise APIError(
            "validation_error",
            "parentRunId is required when fields=light",
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    state = (
        (
            await conn.execute(
                _DELTA_STATE_SQL, {"parent_run_id": parent_run_id, "project_id": project_id}
            )
        )
        .mappings()
        .one()
    )
    etag = _delta_etag(parent_run_id, state["n"], state["max_updated_at"])
    if if_none_match == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})

    parent_row = (await conn.execute(_RUN_DETAIL_SQL, {"id": parent_run_id})).mappings().first()
    if parent_row is None:
        raise APIError("not_found", "run not found", status.HTTP_404_NOT_FOUND)
    ensure_project_match(parent_row["project_id"], project_id)
    aggregate = await _compute_aggregate(conn, parent_row)
    if aggregate is None:
        # parentRunId named a Call (has its own parent_run_id), not a Test
        # Run -- there's nothing to page through under it.
        raise APIError(
            "validation_error",
            "parentRunId must name a Test Run, not a Call",
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    rows = (
        (
            await conn.execute(
                _DELTA_ROWS_SQL,
                {
                    "parent_run_id": parent_run_id,
                    "project_id": project_id,
                    "since": since,
                    "cap": _DELTA_ROW_CAP,
                },
            )
        )
        .mappings()
        .all()
    )
    response.headers["ETag"] = etag
    return RunsDelta(calls=[_run_delta_row(row) for row in rows], aggregate=aggregate)


@router.get("/v1/runs", response_model=list[DashboardRunRow] | RunsDelta)
async def list_runs(
    request: Request,
    response: Response,
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
    type: str | None = Query(default=None),  # noqa: A002
    suite_id: uuid.UUID | None = Query(default=None, alias="suiteId"),
    agent_id: uuid.UUID | None = Query(default=None, alias="agentId"),
    status: str | None = Query(default=None),  # noqa: A002
    is_parent: bool | None = Query(default=None, alias="isParent"),
    parent_run_id: uuid.UUID | None = Query(default=None, alias="parentRunId"),
    since: datetime | None = Query(default=None),
    fields: Literal["light"] | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[DashboardRunRow] | RunsDelta | Response:
    if fields == "light":
        async with engine.connect() as conn:
            return await _run_delta(
                conn,
                response,
                project_id=project_id,
                parent_run_id=parent_run_id,
                since=since,
                if_none_match=request.headers.get("if-none-match"),
            )

    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    _LIST_RUNS_SQL,
                    {
                        "project_id": project_id,
                        "type": type,
                        "agent_id": agent_id,
                        "status": status,
                        "suite_id": suite_id,
                        "is_parent": is_parent,
                        "parent_run_id": parent_run_id,
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
    "       r.project_id, r.end_reason, r.cost, r.recording_url, r.parent_run_id, "
    "       r.scenario_id, sc.persona, "
    "       a.name AS agent_name, a.transport, a.language, "
    "       sc.name AS scenario_name, s.name AS suite_name "
    "FROM runs r "
    "JOIN agents a ON a.id = r.agent_id "
    "LEFT JOIN scenarios sc ON sc.id = r.scenario_id "
    "LEFT JOIN suites s ON s.id = sc.suite_id "
    "WHERE r.id = :id"
)

_RUN_EVENTS_SQL = text("SELECT type, data FROM run_events WHERE run_id = :run_id ORDER BY seq")

_CHILDREN_FOR_AGGREGATE_SQL = text(
    "SELECT r.id, r.status, r.metrics, r.cost, r.scenario_id, sc.persona "
    "FROM runs r LEFT JOIN scenarios sc ON sc.id = r.scenario_id "
    "WHERE r.parent_run_id = :parent_id"
)

_LATENCY_PERCENTILES_SQL = text(
    "SELECT "
    "  percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50, "
    "  percentile_cont(0.9) WITHIN GROUP (ORDER BY latency_ms) AS p90, "
    "  percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95 "
    "FROM turns WHERE run_id IN :run_ids AND latency_ms IS NOT NULL"
).bindparams(bindparam("run_ids", expanding=True))


def _reduce_aggregate(rows: list[dict[str, Any]]) -> RunAggregate:
    """The part of the aggregate that doesn't need a DB round trip (latency
    percentiles are the exception -- see `_compute_aggregate`) -- kept
    separate so a property test can call it directly against hand-built
    fixtures without a test DB."""
    status_counts: Counter[str] = Counter(row["status"] for row in rows)
    verdicts = [verdict_for_run(row["status"], row["metrics"]) for row in rows]
    scored = [v for v in verdicts if v != "idle"]
    pass_rate = (sum(1 for v in scored if v == "pass") / len(scored)) if scored else None

    total_cost: float | None = None
    for row in rows:
        cost = row.get("cost") or {}
        est = cost.get("estUsd")
        if isinstance(est, int | float):
            total_cost = (total_cost or 0.0) + float(est)

    scenario_ids = {row["scenario_id"] for row in rows if row["scenario_id"] is not None}
    personas = {row["persona"] for row in rows if row.get("persona")}
    return RunAggregate(
        call_count=len(rows),
        status_counts=dict(status_counts),
        pass_rate=pass_rate,
        latency_p50_ms=None,
        latency_p90_ms=None,
        latency_p95_ms=None,
        total_cost_usd=total_cost,
        distinct_scenario_count=len(scenario_ids),
        distinct_persona_count=len(personas),
    )


async def _compute_aggregate(conn: AsyncConnection, run_row: RowMapping) -> RunAggregate | None:
    """B2.5-03: only a Test Run (parent_run_id IS NULL) gets an aggregate --
    a Call is itself a leaf. Reduces over real children when they exist;
    otherwise treats the run as its own sole implicit call, per the
    ticket's "do not special-case a single simulation run" rule."""
    if run_row["parent_run_id"] is not None:
        return None

    children = (
        (await conn.execute(_CHILDREN_FOR_AGGREGATE_SQL, {"parent_id": run_row["id"]}))
        .mappings()
        .all()
    )
    rows: list[dict[str, Any]] = (
        [dict(row) for row in children]
        if children
        else [
            {
                "id": run_row["id"],
                "status": run_row["status"],
                "metrics": run_row["metrics"],
                "cost": run_row["cost"],
                "scenario_id": run_row["scenario_id"],
                "persona": run_row["persona"],
            }
        ]
    )
    aggregate = _reduce_aggregate(rows)

    run_ids = [row["id"] for row in rows]
    latency_row = (
        (await conn.execute(_LATENCY_PERCENTILES_SQL, {"run_ids": run_ids})).mappings().one()
    )
    return aggregate.model_copy(
        update={
            "latency_p50_ms": latency_row["p50"],
            "latency_p90_ms": latency_row["p90"],
            "latency_p95_ms": latency_row["p95"],
        }
    )


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
async def get_run(
    run_id: uuid.UUID,
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
) -> RunDetail:
    async with engine.connect() as conn:
        row = (await conn.execute(_RUN_DETAIL_SQL, {"id": run_id})).mappings().first()
        if row is None:
            raise APIError("not_found", "run not found", status.HTTP_404_NOT_FOUND)
        ensure_project_match(row["project_id"], project_id)
        events = (await conn.execute(_RUN_EVENTS_SQL, {"run_id": run_id})).mappings().all()
        aggregate = await _compute_aggregate(conn, row)

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
        project_id=str(row["project_id"]),
        end_reason=row["end_reason"],
        cost=_run_cost(row["cost"]),
        recording_url=(
            signed_recording_url(row["recording_url"]) if row["recording_url"] else None
        ),
        created_at=_isoformat(row["created_at"]),
        transcript=transcript,
        result_assertions=result_assertions,
        latency_series=latency_series,
        is_parent=row["parent_run_id"] is None,
        parent_run_id=str(row["parent_run_id"]) if row["parent_run_id"] is not None else None,
        aggregate=aggregate,
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
    "INSERT INTO runs (id, project_id, type, agent_id, scenario_id, config, idempotency_key, "
    " created_by_user_id) "
    "VALUES (:id, :project_id, :type, :agent_id, :scenario_id, CAST(:config AS jsonb), "
    " :idempotency_key, :user_id) "
    # B2.5-01: matches the composite UNIQUE(project_id, idempotency_key) --
    # migration 005's fix for the cross-project collision this used to
    # allow when the constraint (and this conflict target) were global.
    "ON CONFLICT (project_id, idempotency_key) DO NOTHING "
    "RETURNING id"
)


async def _create_run(
    engine: AsyncEngine,
    *,
    project_id: uuid.UUID,
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
    NULLs equal), so unkeyed requests always insert a fresh row.

    `agent_id` is looked up scoped to `project_id` (B2.5-01: hard scoping)
    -- an agent from another project 404s exactly like one that doesn't
    exist, never leaking that it belongs to someone else."""
    async with engine.connect() as conn, conn.begin():
        agent_row = (
            (
                await conn.execute(
                    text(
                        "SELECT max_concurrency FROM agents "
                        "WHERE id = :id AND project_id = :project_id"
                    ),
                    {"id": agent_id, "project_id": project_id},
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
                        "project_id": project_id,
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
                    text(
                        "SELECT id FROM runs WHERE project_id = :project_id "
                        "AND idempotency_key = :key"
                    ),
                    {"project_id": project_id, "key": idempotency_key},
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
    project_id: uuid.UUID = Depends(require_project_id),
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
                        "JOIN suites s ON s.id = sc.suite_id "
                        "WHERE sc.id = :id AND s.project_id = :project_id"
                    ),
                    {"id": scenario_id, "project_id": project_id},
                )
            )
            .mappings()
            .first()
        )
    if scenario_row is None:
        raise APIError("not_found", "scenario not found", status.HTTP_404_NOT_FOUND)

    run_id = await _create_run(
        engine,
        project_id=project_id,
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
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> RunCreateResponse:
    agent_id = uuid.UUID(body.agent_id)
    _validate_dummy_identity(body.dummy_identity)

    run_id = await _create_run(
        engine,
        project_id=project_id,
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
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> RunCreateResponse:
    agent_id = uuid.UUID(body.agent_id)

    run_id = await _create_run(
        engine,
        project_id=project_id,
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
    project_id: uuid.UUID = Depends(require_project_id),
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
                    text("SELECT status, project_id FROM runs WHERE id = :id FOR UPDATE"),
                    {"id": run_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise APIError("not_found", "run not found", status.HTTP_404_NOT_FOUND)
        ensure_project_match(row["project_id"], project_id)
        if row["status"] not in _CANCELLABLE_STATUSES:
            raise APIError(
                "conflict", "run has already reached a terminal state", status.HTTP_409_CONFLICT
            )
        await conn.execute(
            text("UPDATE runs SET status = 'cancelled' WHERE id = :id"), {"id": run_id}
        )
        await maybe_close_parent(conn, run_id)
        await conn.execute(text("SELECT pg_notify('run_events', :run_id)"), {"run_id": str(run_id)})


@router.post(
    "/v1/runs/{run_id}/rerun",
    response_model=RunCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def rerun_run(
    run_id: uuid.UUID,
    user_id: str = Depends(require_user_id),
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
) -> RunCreateResponse:
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT type, agent_id, scenario_id, config, project_id "
                        "FROM runs WHERE id = :id"
                    ),
                    {"id": run_id},
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        raise APIError("not_found", "run not found", status.HTTP_404_NOT_FOUND)
    ensure_project_match(row["project_id"], project_id)

    new_run_id = await _create_run(
        engine,
        project_id=project_id,
        run_type=row["type"],
        agent_id=row["agent_id"],
        scenario_id=row["scenario_id"],
        config=row["config"] or {},
        idempotency_key=None,
        user_id=user_id,
    )
    return RunCreateResponse(run_id=str(new_run_id))
