"""B1-02: /v1/agents CRUD + test-connection (stubbed) + health.

No frontend form exists yet for agent creation, so the per-transport
config shapes (WebConfig/SipConfig/PhoneConfig in app/schemas/agents.py)
and this router's design choices are this repo's own, not pinned to a
provided type:

- `GET /v1/agents` returns `ConnectedAgent[]` (the Agents-screen table
  row shape); `GET/POST/PATCH /v1/agents{,/{id}}` return the fuller
  `AgentDetail` (config, language, max_concurrency) needed by a
  create/edit form. `GET /v1/agents/health` returns `AgentHealth[]`.
- `status`/`last_seen_at` are real DB columns, but nothing populates
  them yet -- B3-02 (agent liveness) is the ticket that actually
  probes agents periodically. Until then every agent reports
  ConnectionStatus "Offline" (conservative: absence of a health signal
  is not evidence of health) and AgentHealth.uptime "Never probed".
- `POST /v1/agents/test-connection` (pre-save) and
  `POST /v1/agents/{id}/test-connection` (post-save) both return a
  canned success for `web` and `501 not_supported` for `sip`/`phone` --
  matching B3-01's real ticket verbatim ("SIP/Phone tabs return 501
  not_supported"), not a temporary stub for those two transports.
- Deleting an agent referenced by any suite or run 409s rather than
  crashing on the FK (agents.id has no ON DELETE policy, deliberately:
  unlike scenarios->runs, silently cascading a delete through an
  agent's suites would be much more destructive than the deletor
  likely intends).
"""

import json
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, status
from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.db import get_engine
from app.deps import ensure_project_match, require_project_id, require_user_id
from app.errors import APIError
from app.formatting import relative_time
from app.schemas.agents import (
    AgentConfig,
    AgentCreate,
    AgentDetail,
    AgentHealth,
    AgentUpdate,
    ConnectedAgent,
    ConnectionStatus,
    ConnectionTag,
    TestConnectionResult,
)

router = APIRouter(tags=["agents"])

_TAG_BY_TRANSPORT: dict[str, ConnectionTag] = {"web": "RTC", "sip": "SIP", "phone": "TEL"}
_TRANSPORT_LABEL: dict[str, str] = {"web": "Web", "sip": "SIP", "phone": "Phone"}


def _status_label(raw_status: str | None) -> ConnectionStatus:
    if raw_status == "healthy":
        return "Healthy"
    if raw_status == "degraded":
        return "Degraded"
    return "Offline"


def _dot_color(conn_status: ConnectionStatus) -> str:
    return {"Healthy": "green", "Degraded": "yellow", "Offline": "red"}[conn_status]


def _meta(transport: str, language: str | None) -> str:
    label = _TRANSPORT_LABEL.get(transport, transport)
    return f"{label} · {language}" if language else label


def _connected_agent(row: RowMapping) -> ConnectedAgent:
    return ConnectedAgent(
        id=str(row["id"]),
        name=row["name"],
        meta=_meta(row["transport"], row["language"]),
        tag=_TAG_BY_TRANSPORT.get(row["transport"], "RTC"),
        status=_status_label(row["status"]),
    )


def _agent_health(row: RowMapping) -> AgentHealth:
    conn_status = _status_label(row["status"])
    uptime = "Never probed" if row["last_seen_at"] is None else relative_time(row["last_seen_at"])
    return AgentHealth(
        id=str(row["id"]),
        name=row["name"],
        meta=_meta(row["transport"], row["language"]),
        uptime=uptime,
        dot_color=_dot_color(conn_status),
    )


def _agent_detail(row: RowMapping) -> AgentDetail:
    return AgentDetail(
        id=str(row["id"]),
        name=row["name"],
        project_id=str(row["project_id"]),
        transport=row["transport"],
        config=row["config"],
        language=row["language"],
        max_concurrency=row["max_concurrency"],
        status=row["status"],
        last_seen_at=row["last_seen_at"].isoformat() if row["last_seen_at"] else None,
    )


_AGENT_COLUMNS = (
    "id, project_id, name, transport, config, language, max_concurrency, status, last_seen_at"
)


@router.get("/v1/agents", response_model=list[ConnectedAgent])
async def list_agents(
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
) -> list[ConnectedAgent]:
    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        f"SELECT {_AGENT_COLUMNS} FROM agents "
                        "WHERE project_id = :project_id ORDER BY created_at"
                    ),
                    {"project_id": project_id},
                )
            )
            .mappings()
            .all()
        )
    return [_connected_agent(row) for row in rows]


@router.get("/v1/agents/health", response_model=list[AgentHealth])
async def agents_health(
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
) -> list[AgentHealth]:
    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        f"SELECT {_AGENT_COLUMNS} FROM agents "
                        "WHERE project_id = :project_id ORDER BY created_at"
                    ),
                    {"project_id": project_id},
                )
            )
            .mappings()
            .all()
        )
    return [_agent_health(row) for row in rows]


@router.post("/v1/agents", response_model=AgentDetail, status_code=status.HTTP_201_CREATED)
async def create_agent(
    body: AgentCreate,
    user_id: str = Depends(require_user_id),
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
) -> AgentDetail:
    config_dict = body.config.model_dump(mode="json", by_alias=True)
    async with engine.connect() as conn, conn.begin():
        row = (
            (
                await conn.execute(
                    text(
                        "INSERT INTO agents "
                        "(id, project_id, name, transport, config, language, max_concurrency, "
                        " created_by_user_id) "
                        "VALUES (:id, :project_id, :name, :transport, CAST(:config AS jsonb), "
                        " :language, :max_concurrency, :user_id) "
                        f"RETURNING {_AGENT_COLUMNS}"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "project_id": project_id,
                        "name": body.name,
                        "transport": body.config.transport,
                        "config": _dump_json(config_dict),
                        "language": body.language,
                        "max_concurrency": body.max_concurrency,
                        "user_id": user_id,
                    },
                )
            )
            .mappings()
            .one()
        )
    return _agent_detail(row)


def _dump_json(value: object) -> str:
    return json.dumps(value)


async def _fetch_agent_or_404(
    conn: AsyncConnection, agent_id: uuid.UUID, project_id: uuid.UUID
) -> RowMapping:
    row = (
        (
            await conn.execute(
                text(f"SELECT {_AGENT_COLUMNS} FROM agents WHERE id = :id"), {"id": agent_id}
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise APIError("not_found", "agent not found", status.HTTP_404_NOT_FOUND)
    ensure_project_match(row["project_id"], project_id)
    return row


@router.get("/v1/agents/{agent_id}", response_model=AgentDetail)
async def get_agent(
    agent_id: uuid.UUID,
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
) -> AgentDetail:
    async with engine.connect() as conn:
        row = await _fetch_agent_or_404(conn, agent_id, project_id)
    return _agent_detail(row)


@router.patch("/v1/agents/{agent_id}", response_model=AgentDetail)
async def update_agent(
    agent_id: uuid.UUID,
    body: AgentUpdate,
    user_id: str = Depends(require_user_id),
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
) -> AgentDetail:
    updates: dict[str, Any] = {}
    if body.name is not None:
        updates["name"] = body.name
    if body.language is not None:
        updates["language"] = body.language
    if body.max_concurrency is not None:
        updates["max_concurrency"] = body.max_concurrency
    if body.config is not None:
        updates["transport"] = body.config.transport
        updates["config"] = _dump_json(body.config.model_dump(mode="json", by_alias=True))

    async with engine.connect() as conn, conn.begin():
        await _fetch_agent_or_404(conn, agent_id, project_id)
        if updates:
            assignments = []
            params: dict[str, Any] = {"id": agent_id}
            for col, val in updates.items():
                clause = f"{col} = CAST(:{col} AS jsonb)" if col == "config" else f"{col} = :{col}"
                assignments.append(clause)
                params[col] = val
            await conn.execute(
                text(f"UPDATE agents SET {', '.join(assignments)} WHERE id = :id"), params
            )
        row = (
            (
                await conn.execute(
                    text(f"SELECT {_AGENT_COLUMNS} FROM agents WHERE id = :id"), {"id": agent_id}
                )
            )
            .mappings()
            .one()
        )
    return _agent_detail(row)


@router.delete("/v1/agents/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: uuid.UUID,
    user_id: str = Depends(require_user_id),
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
) -> None:
    async with engine.connect() as conn, conn.begin():
        await _fetch_agent_or_404(conn, agent_id, project_id)
        in_use = (
            await conn.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM suites WHERE agent_id = :id) "
                    "+ (SELECT count(*) FROM runs WHERE agent_id = :id) AS n"
                ),
                {"id": agent_id},
            )
        ).scalar_one()
        if in_use:
            raise APIError(
                "conflict",
                "agent has associated suites or runs and cannot be deleted",
                status.HTTP_409_CONFLICT,
            )
        await conn.execute(text("DELETE FROM agents WHERE id = :id"), {"id": agent_id})


def _test_connection_result(transport: Literal["web", "sip", "phone"]) -> TestConnectionResult:
    if transport != "web":
        raise APIError(
            "not_supported",
            f"{transport} test-connection is not yet supported",
            status.HTTP_501_NOT_IMPLEMENTED,
        )
    return TestConnectionResult(success=True, rtt_ms=42, detail="stub result -- real in B3-01")


@router.post("/v1/agents/test-connection", response_model=TestConnectionResult)
async def test_connection_pre_save(body: AgentConfig) -> TestConnectionResult:
    return _test_connection_result(body.transport)


@router.post("/v1/agents/{agent_id}/test-connection", response_model=TestConnectionResult)
async def test_connection_post_save(
    agent_id: uuid.UUID,
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
) -> TestConnectionResult:
    async with engine.connect() as conn:
        row = await _fetch_agent_or_404(conn, agent_id, project_id)
    return _test_connection_result(row["transport"])
