import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import bindparam, text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import get_engine
from app.deps import require_user_id
from app.errors import APIError
from app.formatting import relative_time
from app.schemas.suites import (
    ScenarioCreateRequest,
    ScenarioSummary,
    ScenarioUpdate,
    SuiteCreate,
    SuiteDetail,
    SuiteListItem,
    SuiteUpdate,
)
from app.verdict import Verdict, format_score, verdict_for_run

router = APIRouter(tags=["suites"])


@dataclass
class _LatestRun:
    run_id: uuid.UUID
    status: str
    metrics: dict[str, Any] | None
    created_at: datetime


_LATEST_RUNS_BY_SCENARIO_SQL = text(
    "SELECT DISTINCT ON (scenario_id) scenario_id, id, status, metrics, created_at "
    "FROM runs WHERE scenario_id IN :scenario_ids "
    "ORDER BY scenario_id, created_at DESC"
).bindparams(bindparam("scenario_ids", expanding=True))


async def _fetch_latest_runs_by_scenario(
    engine: AsyncEngine, scenario_ids: list[uuid.UUID]
) -> dict[uuid.UUID, _LatestRun]:
    if not scenario_ids:
        return {}
    async with engine.connect() as conn:
        result = await conn.execute(_LATEST_RUNS_BY_SCENARIO_SQL, {"scenario_ids": scenario_ids})
        return {
            row["scenario_id"]: _LatestRun(
                run_id=row["id"],
                status=row["status"],
                metrics=row["metrics"],
                created_at=row["created_at"],
            )
            for row in result.mappings().all()
        }


def _verdict_and_score(latest: _LatestRun | None) -> tuple[Verdict, str]:
    if latest is None:
        return "idle", "-"
    score = (latest.metrics or {}).get("score")
    return verdict_for_run(latest.status, latest.metrics), format_score(score)


def _derive_initials(persona: str) -> str:
    words = persona.split()
    if not words:
        return "??"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


def _assert_count(assertions: object) -> int:
    return len(assertions) if isinstance(assertions, list) else 0


def _scenario_summary(
    row: RowMapping, latest: _LatestRun | None, assert_count: int
) -> ScenarioSummary:
    verdict, score = _verdict_and_score(latest)
    return ScenarioSummary(
        id=str(row["id"]),
        suite_id=str(row["suite_id"]),
        name=row["name"],
        persona=row["persona"],
        assert_count=assert_count,
        status=verdict,
        score=score,
        run_id=str(latest.run_id) if latest else "",
    )


def _suite_list_item(
    suite_row: RowMapping,
    scenario_ids: list[uuid.UUID],
    latest_by_scenario: dict[uuid.UUID, _LatestRun],
) -> SuiteListItem:
    latests = [latest_by_scenario.get(sid) for sid in scenario_ids]
    verdicts = [_verdict_and_score(latest)[0] for latest in latests]
    scored = [v for v in verdicts if v != "idle"]
    pr = round(100 * sum(1 for v in scored if v == "pass") / len(scored)) if scored else 0
    run_times = [latest.created_at for latest in latests if latest is not None]
    last_run = max(run_times) if run_times else None
    return SuiteListItem(
        id=str(suite_row["id"]),
        name=suite_row["name"],
        desc=suite_row["description"] or "",
        agent=suite_row["agent_name"],
        last_run=relative_time(last_run),
        pass_rate=f"{pr}%",
        pr=pr,
        count=len(scenario_ids),
    )


_SUITES_SQL = text(
    "SELECT s.id, s.name, s.description, a.name AS agent_name "
    "FROM suites s JOIN agents a ON a.id = s.agent_id "
    "ORDER BY s.created_at"
)
_SCENARIOS_ALL_SQL = text(
    "SELECT id, suite_id, name, persona, assertions FROM scenarios ORDER BY suite_id, created_at"
)
_SUITE_BY_ID_SQL = text(
    "SELECT s.id, s.name, s.description, a.name AS agent_name "
    "FROM suites s JOIN agents a ON a.id = s.agent_id WHERE s.id = :id"
)
_SCENARIOS_FOR_SUITE_SQL = text(
    "SELECT id, suite_id, name, persona, assertions FROM scenarios "
    "WHERE suite_id = :suite_id ORDER BY created_at"
)


@router.get("/v1/suites", response_model=list[SuiteListItem])
async def list_suites(engine: AsyncEngine = Depends(get_engine)) -> list[SuiteListItem]:
    async with engine.connect() as conn:
        suites = (await conn.execute(_SUITES_SQL)).mappings().all()
        scenarios = (await conn.execute(_SCENARIOS_ALL_SQL)).mappings().all()

    scenarios_by_suite: dict[uuid.UUID, list[uuid.UUID]] = {}
    for row in scenarios:
        scenarios_by_suite.setdefault(row["suite_id"], []).append(row["id"])

    all_scenario_ids = [row["id"] for row in scenarios]
    latest_by_scenario = await _fetch_latest_runs_by_scenario(engine, all_scenario_ids)

    return [
        _suite_list_item(suite, scenarios_by_suite.get(suite["id"], []), latest_by_scenario)
        for suite in suites
    ]


def _parse_uuid(value: str, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise APIError(
            "validation_error",
            f"{field} must be a valid UUID",
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        ) from exc


@router.post("/v1/suites", response_model=SuiteListItem, status_code=status.HTTP_201_CREATED)
async def create_suite(
    body: SuiteCreate,
    user_id: str = Depends(require_user_id),
    engine: AsyncEngine = Depends(get_engine),
) -> SuiteListItem:
    agent_id = _parse_uuid(body.agent_id, "agentId")
    async with engine.connect() as conn, conn.begin():
        agent_row = (
            (await conn.execute(text("SELECT name FROM agents WHERE id = :id"), {"id": agent_id}))
            .mappings()
            .first()
        )
        if agent_row is None:
            raise APIError("not_found", "agent not found", status.HTTP_404_NOT_FOUND)
        row = (
            (
                await conn.execute(
                    text(
                        "INSERT INTO suites (id, name, description, agent_id, created_by_user_id) "
                        "VALUES (:id, :name, :description, :agent_id, :user_id) "
                        "RETURNING id, name, description"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "name": body.name,
                        "description": body.description,
                        "agent_id": agent_id,
                        "user_id": user_id,
                    },
                )
            )
            .mappings()
            .one()
        )
    return SuiteListItem(
        id=str(row["id"]),
        name=row["name"],
        desc=row["description"] or "",
        agent=agent_row["name"],
        last_run="Never",
        pass_rate="0%",
        pr=0,
        count=0,
    )


@router.get("/v1/suites/{suite_id}", response_model=SuiteDetail)
async def get_suite(suite_id: uuid.UUID, engine: AsyncEngine = Depends(get_engine)) -> SuiteDetail:
    async with engine.connect() as conn:
        suite_row = (await conn.execute(_SUITE_BY_ID_SQL, {"id": suite_id})).mappings().first()
        if suite_row is None:
            raise APIError("not_found", "suite not found", status.HTTP_404_NOT_FOUND)
        scenario_rows = (
            (await conn.execute(_SCENARIOS_FOR_SUITE_SQL, {"suite_id": suite_id})).mappings().all()
        )

    scenario_ids = [row["id"] for row in scenario_rows]
    latest_by_scenario = await _fetch_latest_runs_by_scenario(engine, scenario_ids)

    scenarios = [
        _scenario_summary(row, latest_by_scenario.get(row["id"]), _assert_count(row["assertions"]))
        for row in scenario_rows
    ]
    list_item = _suite_list_item(suite_row, scenario_ids, latest_by_scenario)
    return SuiteDetail(**list_item.model_dump(by_alias=False), scenarios=scenarios)


@router.patch("/v1/suites/{suite_id}", response_model=SuiteDetail)
async def update_suite(
    suite_id: uuid.UUID,
    body: SuiteUpdate,
    user_id: str = Depends(require_user_id),
    engine: AsyncEngine = Depends(get_engine),
) -> SuiteDetail:
    updates: dict[str, Any] = {}
    if body.name is not None:
        updates["name"] = body.name
    if body.description is not None:
        updates["description"] = body.description
    if body.agent_id is not None:
        updates["agent_id"] = _parse_uuid(body.agent_id, "agentId")

    async with engine.connect() as conn, conn.begin():
        exists = (
            await conn.execute(text("SELECT 1 FROM suites WHERE id = :id"), {"id": suite_id})
        ).first()
        if exists is None:
            raise APIError("not_found", "suite not found", status.HTTP_404_NOT_FOUND)
        if updates:
            set_clause = ", ".join(f"{col} = :{col}" for col in updates)
            await conn.execute(
                text(f"UPDATE suites SET {set_clause} WHERE id = :id"),
                {**updates, "id": suite_id},
            )
    return await get_suite(suite_id, engine)


@router.delete("/v1/suites/{suite_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_suite(
    suite_id: uuid.UUID,
    user_id: str = Depends(require_user_id),
    engine: AsyncEngine = Depends(get_engine),
) -> None:
    async with engine.connect() as conn, conn.begin():
        result = await conn.execute(text("DELETE FROM suites WHERE id = :id"), {"id": suite_id})
        if result.rowcount == 0:
            raise APIError("not_found", "suite not found", status.HTTP_404_NOT_FOUND)


@router.post(
    "/v1/suites/{suite_id}/scenarios",
    response_model=ScenarioSummary,
    status_code=status.HTTP_201_CREATED,
)
async def add_scenario(
    suite_id: uuid.UUID,
    body: ScenarioCreateRequest,
    response: Response,
    user_id: str = Depends(require_user_id),
    engine: AsyncEngine = Depends(get_engine),
) -> ScenarioSummary:
    async with engine.connect() as conn, conn.begin():
        suite_exists = (
            await conn.execute(text("SELECT 1 FROM suites WHERE id = :id"), {"id": suite_id})
        ).first()
        if suite_exists is None:
            raise APIError("not_found", "suite not found", status.HTTP_404_NOT_FOUND)

        if body.from_draft_id is not None:
            existing = (
                (
                    await conn.execute(
                        text(
                            "SELECT id, suite_id, name, persona, assertions FROM scenarios "
                            "WHERE source_draft_ref = :ref"
                        ),
                        {"ref": body.from_draft_id},
                    )
                )
                .mappings()
                .first()
            )
            if existing is not None:
                response.status_code = status.HTTP_200_OK
                return _scenario_summary(existing, None, _assert_count(existing["assertions"]))

            # B5 hasn't landed yet (discovery_drafts.draft_id is only unique
            # per-run, per spine §3's composite PK) -- this lookup assumes
            # draft_id is effectively globally addressable, which only holds
            # once B5's explorer generates non-colliding draft ids.
            draft = (
                (
                    await conn.execute(
                        text(
                            "SELECT name, persona, assertions FROM discovery_drafts "
                            "WHERE draft_id = :draft_id LIMIT 1"
                        ),
                        {"draft_id": body.from_draft_id},
                    )
                )
                .mappings()
                .first()
            )
            if draft is None:
                raise APIError("not_found", "draft not found", status.HTTP_404_NOT_FOUND)
            name, persona, assertions = draft["name"], draft["persona"], draft["assertions"]
            source, source_draft_ref = "discovery_draft", body.from_draft_id
            script = None
        else:
            assert body.name is not None
            assert body.persona is not None
            name, persona, assertions = body.name, body.persona, body.assertions
            source, source_draft_ref = "manual", None
            script = body.script

        persona_initials = body.persona_initials or _derive_initials(persona)
        row = (
            (
                await conn.execute(
                    text(
                        "INSERT INTO scenarios "
                        "(id, suite_id, name, persona, persona_initials, script, assertions, "
                        " source, source_draft_ref) "
                        "VALUES (:id, :suite_id, :name, :persona, :persona_initials, :script, "
                        " CAST(:assertions AS jsonb), :source, :source_draft_ref) "
                        "RETURNING id, suite_id, name, persona, assertions"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "suite_id": suite_id,
                        "name": name,
                        "persona": persona,
                        "persona_initials": persona_initials,
                        "script": script,
                        "assertions": json.dumps(assertions),
                        "source": source,
                        "source_draft_ref": source_draft_ref,
                    },
                )
            )
            .mappings()
            .one()
        )
    return _scenario_summary(row, None, _assert_count(row["assertions"]))


@router.patch("/v1/scenarios/{scenario_id}", response_model=ScenarioSummary)
async def update_scenario(
    scenario_id: uuid.UUID,
    body: ScenarioUpdate,
    user_id: str = Depends(require_user_id),
    engine: AsyncEngine = Depends(get_engine),
) -> ScenarioSummary:
    updates: dict[str, Any] = {}
    if body.name is not None:
        updates["name"] = body.name
    if body.persona is not None:
        updates["persona"] = body.persona
    if body.script is not None:
        updates["script"] = body.script
    if body.assertions is not None:
        updates["assertions"] = json.dumps(body.assertions)

    async with engine.connect() as conn, conn.begin():
        exists = (
            await conn.execute(text("SELECT 1 FROM scenarios WHERE id = :id"), {"id": scenario_id})
        ).first()
        if exists is None:
            raise APIError("not_found", "scenario not found", status.HTTP_404_NOT_FOUND)
        if updates:
            assignments = []
            params: dict[str, Any] = {"id": scenario_id}
            for col, val in updates.items():
                if col == "assertions":
                    assignments.append(f"{col} = CAST(:{col} AS jsonb)")
                else:
                    assignments.append(f"{col} = :{col}")
                params[col] = val
            await conn.execute(
                text(f"UPDATE scenarios SET {', '.join(assignments)} WHERE id = :id"), params
            )
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT id, suite_id, name, persona, assertions "
                        "FROM scenarios WHERE id = :id"
                    ),
                    {"id": scenario_id},
                )
            )
            .mappings()
            .one()
        )

    latest_by_scenario = await _fetch_latest_runs_by_scenario(engine, [scenario_id])
    return _scenario_summary(
        row, latest_by_scenario.get(scenario_id), _assert_count(row["assertions"])
    )


@router.delete("/v1/scenarios/{scenario_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_scenario(
    scenario_id: uuid.UUID,
    user_id: str = Depends(require_user_id),
    engine: AsyncEngine = Depends(get_engine),
) -> None:
    async with engine.connect() as conn, conn.begin():
        result = await conn.execute(
            text("DELETE FROM scenarios WHERE id = :id"), {"id": scenario_id}
        )
        if result.rowcount == 0:
            raise APIError("not_found", "scenario not found", status.HTTP_404_NOT_FOUND)
