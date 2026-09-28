"""B2.7-04: /v1/metrics CRUD + resolution.

A metric is visible if it's built-in (`project_id IS NULL`, read-only
project-wide, the resolution floor) or belongs to the caller's current
project -- same convention as app/api/personas.py. Unlike personas, there
is no "duplicate to project" here: built-ins are never edited, only
shadowed by a project- or agent-level metric of the same `name` (see
app/engine/metrics/resolver.py for the precedence chain).

`GET /v1/metrics/resolved` is declared before `GET /v1/metrics/{metric_id}`
-- FastAPI/Starlette matches routes in registration order and the default
path converter accepts any string, so `{metric_id}` would otherwise
swallow the literal `/resolved` path and fail Pydantic UUID validation
with a 422 instead of ever reaching the intended handler.
"""

import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.db import get_engine
from app.deps import ensure_project_match, require_project_id, require_user_id
from app.engine.judge.judge import FinalJudge, build_judge_client
from app.engine.metrics.backtest import run_backtest, spec_hash
from app.engine.metrics.resolver import resolve_metrics_for_scenario, source_level
from app.errors import APIError
from app.schemas.metrics import (
    BacktestCallVerdict,
    BacktestRequest,
    BacktestResult,
    MetricCreate,
    MetricDetail,
    MetricUpdate,
    ResolvedMetric,
)
from app.usage import UsageTracker

router = APIRouter(tags=["metrics"])

_METRIC_COLUMNS = (
    "id, project_id, agent_id, name, description, kind, output_type, "
    "spec, sampling_pct, status, version, builtin"
)


def _metric_detail(row: RowMapping) -> MetricDetail:
    return MetricDetail(
        id=str(row["id"]),
        project_id=str(row["project_id"]) if row["project_id"] is not None else None,
        agent_id=str(row["agent_id"]) if row["agent_id"] is not None else None,
        name=row["name"],
        description=row["description"],
        kind=row["kind"],
        output_type=row["output_type"],
        spec=row["spec"],
        sampling_pct=row["sampling_pct"],
        status=row["status"],
        version=row["version"],
        builtin=row["builtin"],
    )


def _dump_json(value: object) -> str:
    return json.dumps(value)


@router.get("/v1/metrics", response_model=list[MetricDetail])
async def list_metrics(
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
) -> list[MetricDetail]:
    """Every metric visible to the project: builtins, every project-level
    metric, and every agent-level metric in the project -- unfiltered by
    what's actually attached to any scenario. This is the authoring/
    management view; `/resolved` is the scenario-scoped effective view."""
    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        f"SELECT {_METRIC_COLUMNS} FROM metrics "
                        "WHERE builtin OR project_id = :project_id "
                        "ORDER BY builtin DESC, agent_id NULLS FIRST, name"
                    ),
                    {"project_id": project_id},
                )
            )
            .mappings()
            .all()
        )
    return [_metric_detail(row) for row in rows]


async def _ensure_agent_in_project(
    conn: AsyncConnection, agent_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    row = (
        (await conn.execute(text("SELECT project_id FROM agents WHERE id = :id"), {"id": agent_id}))
        .mappings()
        .first()
    )
    if row is None:
        raise APIError("not_found", "agent not found", status.HTTP_404_NOT_FOUND)
    ensure_project_match(row["project_id"], project_id)


@router.post("/v1/metrics", response_model=MetricDetail, status_code=status.HTTP_201_CREATED)
async def create_metric(
    body: MetricCreate,
    user_id: str = Depends(require_user_id),
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
) -> MetricDetail:
    agent_id = uuid.UUID(body.agent_id) if body.agent_id is not None else None
    async with engine.connect() as conn, conn.begin():
        if agent_id is not None:
            await _ensure_agent_in_project(conn, agent_id, project_id)
        row = (
            (
                await conn.execute(
                    text(
                        "INSERT INTO metrics "
                        "(id, project_id, agent_id, name, description, kind, output_type, "
                        " spec, sampling_pct, created_by_user_id) "
                        "VALUES (:id, :project_id, :agent_id, :name, :description, :kind, "
                        " :output_type, CAST(:spec AS jsonb), :sampling_pct, :user_id) "
                        f"RETURNING {_METRIC_COLUMNS}"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "project_id": project_id,
                        "agent_id": agent_id,
                        "name": body.name,
                        "description": body.description,
                        "kind": body.kind,
                        "output_type": body.output_type,
                        "spec": _dump_json(body.spec),
                        "sampling_pct": body.sampling_pct,
                        "user_id": user_id,
                    },
                )
            )
            .mappings()
            .one()
        )
    return _metric_detail(row)


@router.get("/v1/metrics/resolved", response_model=list[ResolvedMetric])
async def get_resolved_metrics(
    scenario_id: uuid.UUID = Query(alias="scenarioId"),
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
) -> list[ResolvedMetric]:
    async with engine.connect() as conn:
        rows = await resolve_metrics_for_scenario(
            conn, scenario_id=scenario_id, project_id=project_id
        )
    return [
        ResolvedMetric(
            id=str(row["id"]),
            name=row["name"],
            kind=row["kind"],
            output_type=row["output_type"],
            spec=row["spec"],
            sampling_pct=row["sampling_pct"],
            version=row["version"],
            source_level=source_level(row),
        )
        for row in rows
    ]


async def _fetch_metric_or_404(
    conn: AsyncConnection, metric_id: uuid.UUID, project_id: uuid.UUID
) -> RowMapping:
    row = (
        (
            await conn.execute(
                text(f"SELECT {_METRIC_COLUMNS} FROM metrics WHERE id = :id"), {"id": metric_id}
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise APIError("not_found", "metric not found", status.HTTP_404_NOT_FOUND)
    if row["project_id"] is not None:
        ensure_project_match(row["project_id"], project_id)
    return row


@router.get("/v1/metrics/{metric_id}", response_model=MetricDetail)
async def get_metric(
    metric_id: uuid.UUID,
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
) -> MetricDetail:
    async with engine.connect() as conn:
        row = await _fetch_metric_or_404(conn, metric_id, project_id)
    return _metric_detail(row)


@router.patch("/v1/metrics/{metric_id}", response_model=MetricDetail)
async def update_metric(
    metric_id: uuid.UUID,
    body: MetricUpdate,
    user_id: str = Depends(require_user_id),
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
) -> MetricDetail:
    async with engine.connect() as conn, conn.begin():
        existing = await _fetch_metric_or_404(conn, metric_id, project_id)
        if existing["builtin"]:
            raise APIError(
                "builtin_read_only", "built-in metrics cannot be edited", status.HTTP_409_CONFLICT
            )

        if body.status == "active":
            # B2.7-10: bound to the *effective* spec this PATCH would leave
            # the metric with (body.spec if also being changed in this same
            # call, else the existing one) -- draft edits don't bump
            # `version`, so a hash of the actual spec content is the only
            # thing that can catch "backtested spec A, silently activating
            # spec B" in a single request.
            effective_spec = body.spec if body.spec is not None else existing["spec"]
            has_matching_backtest = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM metric_backtests "
                        "WHERE metric_id = :id AND spec_hash = :spec_hash LIMIT 1"
                    ),
                    {"id": metric_id, "spec_hash": spec_hash(effective_spec)},
                )
            ).first()
            if has_matching_backtest is None:
                raise APIError(
                    "backtest_required",
                    "this metric's current spec has not been backtested -- "
                    "POST /v1/metrics/{id}/backtest first",
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                )

        updates: dict[str, Any] = {}
        for field in ("name", "description", "sampling_pct", "status"):
            value = getattr(body, field)
            if value is not None:
                updates[field] = value
        if body.spec is not None:
            updates["spec"] = body.spec

        # B2.7-04: PATCH on a metric that is (still) active when this edit
        # lands bumps `version` -- the live row's next scoring pass gets a
        # new version stamp, but every metric_results row already written
        # keeps the version that produced it, untouched. A first activation
        # (draft -> active in this same PATCH) is not "editing an active
        # metric" and does not bump version -- nothing has been scored
        # against it yet.
        if updates and existing["status"] == "active":
            updates["version"] = existing["version"] + 1

        if updates:
            assignments = []
            params: dict[str, Any] = {"id": metric_id}
            for col, val in updates.items():
                clause = f"{col} = CAST(:{col} AS jsonb)" if col == "spec" else f"{col} = :{col}"
                assignments.append(clause)
                params[col] = _dump_json(val) if col == "spec" else val
            await conn.execute(
                text(f"UPDATE metrics SET {', '.join(assignments)} WHERE id = :id"), params
            )
        row = (
            (
                await conn.execute(
                    text(f"SELECT {_METRIC_COLUMNS} FROM metrics WHERE id = :id"),
                    {"id": metric_id},
                )
            )
            .mappings()
            .one()
        )
    return _metric_detail(row)


@router.post(
    "/v1/metrics/{metric_id}/backtest",
    response_model=BacktestResult,
    status_code=status.HTTP_201_CREATED,
)
async def backtest_metric(
    metric_id: uuid.UUID,
    body: BacktestRequest,
    user_id: str = Depends(require_user_id),
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
) -> BacktestResult:
    """Runs `metric_id` over up to `sample_size` past completed calls'
    already-recorded transcripts -- no new call is placed (app.engine.
    metrics.backtest). Works on a metric of any status, not just `draft`:
    re-backtesting an `active` metric after an edit is exactly how you'd
    reactivate it once B2.7-04's version-bump-on-edit has moved it, and
    nothing about this endpoint should require demoting it to draft first.
    """
    async with engine.connect() as conn:
        metric_row = await _fetch_metric_or_404(conn, metric_id, project_id)
        usage = UsageTracker()
        # Only an llm_judge-kind metric ever needs a real judge client --
        # building one unconditionally would mean every builtin/python
        # backtest fails in any environment without GCP credentials
        # configured for absolutely no reason (it never gets called).
        judge = (
            FinalJudge(build_judge_client(), usage=usage)
            if metric_row["kind"] == "llm_judge"
            else None
        )
        outcomes = await run_backtest(
            conn,
            judge,
            metric_row,
            project_id=project_id,
            sample_size=body.sample_size,
            filters=body.filters,
        )

    verdicts = [
        BacktestCallVerdict(
            run_id=str(run_id),
            # not_sampled never comes back from a backtest (sampling is a
            # live-scoring concept -- every historical call requested here
            # is actually evaluated); defensively treated as an error
            # rather than silently accepted by a schema that doesn't list it.
            status=outcome.status if outcome.status != "not_sampled" else "error",
            value=outcome.value if isinstance(outcome.value, str | float | bool) else None,
            turn_refs=outcome.turn_refs,
            rationale=outcome.rationale,
        )
        for run_id, outcome in outcomes
    ]

    backtest_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO metric_backtests "
                "(id, metric_id, spec_hash, sample_size, filters, agreement, "
                " labeled_count, cost, created_by_user_id) "
                "VALUES (:id, :metric_id, :spec_hash, :sample_size, "
                " CAST(:filters AS jsonb), NULL, 0, CAST(:cost AS jsonb), :user_id)"
            ),
            {
                "id": backtest_id,
                "metric_id": metric_id,
                "spec_hash": spec_hash(metric_row["spec"]),
                "sample_size": body.sample_size,
                "filters": _dump_json(body.filters),
                "cost": _dump_json(usage.as_dict()),
                "user_id": user_id,
            },
        )

    return BacktestResult(
        id=str(backtest_id),
        verdicts=verdicts,
        agreement=None,
        labeled_count=0,
        cost=usage.as_dict(),
    )


@router.delete("/v1/metrics/{metric_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_metric(
    metric_id: uuid.UUID,
    user_id: str = Depends(require_user_id),
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
) -> None:
    async with engine.connect() as conn, conn.begin():
        existing = await _fetch_metric_or_404(conn, metric_id, project_id)
        if existing["builtin"]:
            raise APIError(
                "builtin_read_only", "built-in metrics cannot be deleted", status.HTTP_409_CONFLICT
            )
        in_use = (
            await conn.execute(
                text("SELECT count(*) FROM metric_results WHERE metric_id = :id"),
                {"id": metric_id},
            )
        ).scalar_one()
        if in_use:
            raise APIError(
                "conflict",
                "metric has scored results and cannot be deleted",
                status.HTTP_409_CONFLICT,
            )
        await conn.execute(text("DELETE FROM metrics WHERE id = :id"), {"id": metric_id})
