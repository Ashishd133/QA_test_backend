"""B2.7-04: the single resolution function every metric consumer shares --
the live `GET /v1/metrics/resolved` endpoint (app/api/metrics.py), B2.7-06's
judge compiler, and B2.7-10's backtest endpoint all call this rather than
re-querying, so "what does this scenario's agent actually see" can never
drift between the live path and the backtest path.

Precedence, by `name`: agent-level metric overrides project-level metric
overrides builtin (the floor). Only `status='active'` rows are eligible --
a `draft` override does not shadow anything live; an `archived` one is
gone entirely. This mirrors app.api.personas' "builtin is the floor,
visible unless a real row of the same name exists at a more specific
level" shape, just resolved by name instead of by id.
"""

import uuid
from typing import Literal

from fastapi import status
from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from app.errors import APIError

_METRIC_COLUMNS = (
    "id, project_id, agent_id, name, description, kind, output_type, "
    "spec, sampling_pct, status, version, builtin"
)

_VISIBLE_METRICS_SQL = text(
    f"SELECT {_METRIC_COLUMNS} FROM metrics "
    "WHERE status = 'active' AND ("
    "  builtin "
    "  OR (project_id = :project_id AND agent_id IS NULL) "
    "  OR (project_id = :project_id AND agent_id = :agent_id)"
    ")"
)

_SCENARIO_SCOPE_SQL = text(
    "SELECT s.project_id, s.agent_id FROM scenarios sc "
    "JOIN suites s ON s.id = sc.suite_id WHERE sc.id = :scenario_id"
)

_LEVEL_RANK = {"builtin": 0, "project": 1, "agent": 2}


def source_level(row: RowMapping) -> Literal["builtin", "project", "agent"]:
    if row["builtin"]:
        return "builtin"
    if row["agent_id"] is not None:
        return "agent"
    return "project"


async def scenario_scope(
    conn: AsyncConnection, scenario_id: uuid.UUID, project_id: uuid.UUID
) -> uuid.UUID:
    """Returns the scenario's `agent_id` after confirming it belongs to the
    caller's project -- 404, not 403, on a cross-tenant scenario id, same
    rule as every other project-scoped resource."""
    row = (await conn.execute(_SCENARIO_SCOPE_SQL, {"scenario_id": scenario_id})).mappings().first()
    if row is None or str(row["project_id"]) != str(project_id):
        raise APIError("not_found", "scenario not found", status.HTTP_404_NOT_FOUND)
    return uuid.UUID(str(row["agent_id"]))


async def resolve_metrics_for_agent(
    conn: AsyncConnection, *, project_id: uuid.UUID, agent_id: uuid.UUID
) -> list[RowMapping]:
    """Every metric visible to a scenario belonging to `agent_id`, already
    deduplicated by `name` per the agent > project > builtin precedence.
    Returns raw rows (plus a synthetic `source_level` not present on the
    table) rather than the response schema, so callers that need the DB
    row directly (06's compiler, 10's backtest) don't have to re-fetch."""
    rows = (
        (await conn.execute(_VISIBLE_METRICS_SQL, {"project_id": project_id, "agent_id": agent_id}))
        .mappings()
        .all()
    )
    by_name: dict[str, RowMapping] = {}
    for row in rows:
        current = by_name.get(row["name"])
        if current is None or _LEVEL_RANK[source_level(row)] > _LEVEL_RANK[source_level(current)]:
            by_name[row["name"]] = row
    return list(by_name.values())


async def resolve_metrics_for_scenario(
    conn: AsyncConnection, *, scenario_id: uuid.UUID, project_id: uuid.UUID
) -> list[RowMapping]:
    agent_id = await scenario_scope(conn, scenario_id, project_id)
    return await resolve_metrics_for_agent(conn, project_id=project_id, agent_id=agent_id)
