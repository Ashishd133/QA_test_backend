import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, Response, status
from sqlalchemy import bindparam, text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.config import get_settings
from app.db import get_engine
from app.deps import ensure_project_match, require_project_id, require_user_id
from app.errors import APIError
from app.formatting import relative_time
from app.schemas.suites import (
    ScenarioCreateRequest,
    ScenarioSummary,
    ScenarioUpdate,
    SuiteCreate,
    SuiteDetail,
    SuiteListItem,
    SuiteRunCreate,
    SuiteRunCreateResponse,
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
        project_id=str(suite_row["project_id"]),
        name=suite_row["name"],
        desc=suite_row["description"] or "",
        agent=suite_row["agent_name"],
        last_run=relative_time(last_run),
        pass_rate=f"{pr}%",
        pr=pr,
        count=len(scenario_ids),
    )


_SUITES_SQL = text(
    "SELECT s.id, s.project_id, s.name, s.description, a.name AS agent_name "
    "FROM suites s JOIN agents a ON a.id = s.agent_id "
    "WHERE s.project_id = :project_id "
    "ORDER BY s.created_at"
)
_SCENARIOS_FOR_PROJECT_SQL = text(
    "SELECT sc.id, sc.suite_id, sc.name, sc.persona, sc.assertions FROM scenarios sc "
    "JOIN suites s ON s.id = sc.suite_id "
    "WHERE s.project_id = :project_id ORDER BY sc.suite_id, sc.created_at"
)
_SUITE_BY_ID_SQL = text(
    "SELECT s.id, s.project_id, s.name, s.description, a.name AS agent_name "
    "FROM suites s JOIN agents a ON a.id = s.agent_id WHERE s.id = :id"
)
_SCENARIOS_FOR_SUITE_SQL = text(
    "SELECT id, suite_id, name, persona, assertions FROM scenarios "
    "WHERE suite_id = :suite_id ORDER BY created_at"
)


@router.get("/v1/suites", response_model=list[SuiteListItem])
async def list_suites(
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
) -> list[SuiteListItem]:
    async with engine.connect() as conn:
        suites = (await conn.execute(_SUITES_SQL, {"project_id": project_id})).mappings().all()
        scenarios = (
            (await conn.execute(_SCENARIOS_FOR_PROJECT_SQL, {"project_id": project_id}))
            .mappings()
            .all()
        )

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


async def _fetch_agent_in_project(
    conn: AsyncConnection, agent_id: uuid.UUID, project_id: uuid.UUID
) -> RowMapping:
    """A suite's agent must live in the same project (B2.5-01: hard
    scoping) -- looking it up unscoped would let a suite silently
    cross-reference another project's agent."""
    agent_row = (
        (
            await conn.execute(
                text("SELECT name FROM agents WHERE id = :id AND project_id = :project_id"),
                {"id": agent_id, "project_id": project_id},
            )
        )
        .mappings()
        .first()
    )
    if agent_row is None:
        raise APIError("not_found", "agent not found", status.HTTP_404_NOT_FOUND)
    return agent_row


@router.post("/v1/suites", response_model=SuiteListItem, status_code=status.HTTP_201_CREATED)
async def create_suite(
    body: SuiteCreate,
    user_id: str = Depends(require_user_id),
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
) -> SuiteListItem:
    agent_id = _parse_uuid(body.agent_id, "agentId")
    async with engine.connect() as conn, conn.begin():
        agent_row = await _fetch_agent_in_project(conn, agent_id, project_id)
        row = (
            (
                await conn.execute(
                    text(
                        "INSERT INTO suites "
                        "(id, project_id, name, description, agent_id, created_by_user_id) "
                        "VALUES (:id, :project_id, :name, :description, :agent_id, :user_id) "
                        "RETURNING id, project_id, name, description"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "project_id": project_id,
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
        project_id=str(row["project_id"]),
        name=row["name"],
        desc=row["description"] or "",
        agent=agent_row["name"],
        last_run="Never",
        pass_rate="0%",
        pr=0,
        count=0,
    )


@router.get("/v1/suites/{suite_id}", response_model=SuiteDetail)
async def get_suite(
    suite_id: uuid.UUID,
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
) -> SuiteDetail:
    async with engine.connect() as conn:
        suite_row = (await conn.execute(_SUITE_BY_ID_SQL, {"id": suite_id})).mappings().first()
        if suite_row is None:
            raise APIError("not_found", "suite not found", status.HTTP_404_NOT_FOUND)
        ensure_project_match(suite_row["project_id"], project_id)
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
    project_id: uuid.UUID = Depends(require_project_id),
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
        existing = (
            (
                await conn.execute(
                    text("SELECT project_id FROM suites WHERE id = :id"), {"id": suite_id}
                )
            )
            .mappings()
            .first()
        )
        if existing is None:
            raise APIError("not_found", "suite not found", status.HTTP_404_NOT_FOUND)
        ensure_project_match(existing["project_id"], project_id)
        if "agent_id" in updates:
            await _fetch_agent_in_project(conn, updates["agent_id"], project_id)
        if updates:
            set_clause = ", ".join(f"{col} = :{col}" for col in updates)
            await conn.execute(
                text(f"UPDATE suites SET {set_clause} WHERE id = :id"),
                {**updates, "id": suite_id},
            )
    return await get_suite(suite_id, project_id, engine)


@router.delete("/v1/suites/{suite_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_suite(
    suite_id: uuid.UUID,
    user_id: str = Depends(require_user_id),
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
) -> None:
    async with engine.connect() as conn, conn.begin():
        result = await conn.execute(
            text("DELETE FROM suites WHERE id = :id AND project_id = :project_id"),
            {"id": suite_id, "project_id": project_id},
        )
        if result.rowcount == 0:
            raise APIError("not_found", "suite not found", status.HTTP_404_NOT_FOUND)


_SCENARIO_IDS_FOR_SUITE_SQL = text(
    "SELECT id FROM scenarios WHERE suite_id = :suite_id AND id IN :ids"
).bindparams(bindparam("ids", expanding=True))

_ALL_SCENARIO_IDS_FOR_SUITE_SQL = text(
    "SELECT id FROM scenarios WHERE suite_id = :suite_id ORDER BY created_at"
)

# B2.6-01: parent carries the Idempotency-Key; ON CONFLICT DO NOTHING mirrors
# app.api.runs._INSERT_RUN_SQL exactly, same composite UNIQUE(project_id,
# idempotency_key) from migration 005. Children (below) get no key of their
# own -- stamping the same key onto every child would collide with each
# other on the very first insert.
_INSERT_SUITE_PARENT_SQL = text(
    "INSERT INTO runs (id, project_id, type, agent_id, config, idempotency_key, "
    " created_by_user_id) "
    "VALUES (:id, :project_id, 'suite', :agent_id, CAST(:config AS jsonb), :idempotency_key, "
    " :user_id) "
    "ON CONFLICT (project_id, idempotency_key) DO NOTHING "
    "RETURNING id"
)

_EXISTING_SUITE_PARENT_BY_KEY_SQL = text(
    "SELECT id FROM runs WHERE project_id = :project_id AND idempotency_key = :key"
)

_CHILD_COUNT_SQL = text("SELECT count(*) FROM runs WHERE parent_run_id = :parent_id")

# type='simulation' (not 'suite') -- these are real single-scenario
# simulation runs and must dispatch through the real executor
# (app.workers.executors.EXECUTORS["simulation"] = run_simulation), exactly
# like a call created via POST /v1/simulations/runs. parent_run_id is what
# makes them Calls under the 'suite' parent rather than standalone Test
# Runs (B2.5-03), and what excludes the parent itself from claim.py's
# candidate scan (B3-03: "childless" claimability).
_INSERT_CHILD_RUN_SQL = text(
    "INSERT INTO runs (id, project_id, type, agent_id, scenario_id, parent_run_id, config, "
    " created_by_user_id) "
    "VALUES (:id, :project_id, 'simulation', :agent_id, :scenario_id, :parent_run_id, "
    " CAST(:config AS jsonb), :user_id)"
)


@router.post(
    "/v1/suites/{suite_id}/run",
    response_model=SuiteRunCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def run_suite(
    suite_id: uuid.UUID,
    body: SuiteRunCreate,
    user_id: str = Depends(require_user_id),
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> SuiteRunCreateResponse:
    """B2.6-01: one parent (`type='suite'`) + the child cross-product, all in
    one transaction. Currently the cross-product is just `scenarioIds` --
    personaIds/conditionProfileIds are 422'd below until B2.7 wires them to
    scenarios, rather than silently accepted and dropped.
    """
    agent_id = _parse_uuid(body.agent_id, "agentId")

    if body.persona_ids or body.condition_profile_ids:
        raise APIError(
            "not_supported",
            "personaIds/conditionProfileIds are not supported yet -- omit them "
            "or pass an empty list",
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    if body.scenario_ids is not None and not body.scenario_ids:
        raise APIError(
            "validation_error",
            "scenarioIds must not be empty when provided -- omit it to run every scenario",
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    async with engine.connect() as conn, conn.begin():
        suite_row = (
            (
                await conn.execute(
                    text("SELECT project_id FROM suites WHERE id = :id"), {"id": suite_id}
                )
            )
            .mappings()
            .first()
        )
        if suite_row is None:
            raise APIError("not_found", "suite not found", status.HTTP_404_NOT_FOUND)
        ensure_project_match(suite_row["project_id"], project_id)
        # Deliberately decoupled from the suite's own configured agent --
        # the batch's agentId is an explicit body field (backlog: run a
        # suite against a different agent variant than the one it's
        # authored against).
        await _fetch_agent_in_project(conn, agent_id, project_id)

        if body.scenario_ids is not None:
            requested_ids = [_parse_uuid(sid, "scenarioIds") for sid in body.scenario_ids]
            found_ids = set(
                (
                    await conn.execute(
                        _SCENARIO_IDS_FOR_SUITE_SQL,
                        {"suite_id": suite_id, "ids": requested_ids},
                    )
                )
                .scalars()
                .all()
            )
            if found_ids != set(requested_ids):
                raise APIError(
                    "not_found",
                    "one or more scenarioIds were not found in this suite",
                    status.HTTP_404_NOT_FOUND,
                )
            scenario_ids = requested_ids
        else:
            scenario_ids = list(
                (
                    await conn.execute(_ALL_SCENARIO_IDS_FOR_SUITE_SQL, {"suite_id": suite_id})
                )
                .scalars()
                .all()
            )
        if not scenario_ids:
            raise APIError(
                "validation_error",
                "suite has no scenarios to run",
                status.HTTP_422_UNPROCESSABLE_CONTENT,
            )

        cap = get_settings().suite_run_batch_cap
        if len(scenario_ids) > cap:
            raise APIError(
                "batch_too_large",
                f"this run would queue {len(scenario_ids)} calls, over the "
                f"cap of {cap}",
                status.HTTP_422_UNPROCESSABLE_CONTENT,
            )

        parent_id = uuid.uuid4()
        inserted = (
            (
                await conn.execute(
                    _INSERT_SUITE_PARENT_SQL,
                    {
                        "id": parent_id,
                        "project_id": project_id,
                        "agent_id": agent_id,
                        "config": json.dumps({"scenarioIds": [str(sid) for sid in scenario_ids]}),
                        "idempotency_key": idempotency_key,
                        "user_id": user_id,
                    },
                )
            )
            .mappings()
            .first()
        )
        if inserted is None:
            # Idempotent replay: Idempotency-Key already names a parent from
            # an earlier call to this endpoint (same project) -- return its
            # real child count rather than queueing a second batch.
            existing = (
                (
                    await conn.execute(
                        _EXISTING_SUITE_PARENT_BY_KEY_SQL,
                        {"project_id": project_id, "key": idempotency_key},
                    )
                )
                .mappings()
                .one()
            )
            existing_id = uuid.UUID(str(existing["id"]))
            call_count = (
                await conn.execute(_CHILD_COUNT_SQL, {"parent_id": existing_id})
            ).scalar_one()
            return SuiteRunCreateResponse(parent_run_id=str(existing_id), call_count=call_count)

        child_ids = [uuid.uuid4() for _ in scenario_ids]
        await conn.execute(
            _INSERT_CHILD_RUN_SQL,
            [
                {
                    "id": child_id,
                    "project_id": project_id,
                    "agent_id": agent_id,
                    "scenario_id": scenario_id,
                    "parent_run_id": parent_id,
                    "config": json.dumps({}),
                    "user_id": user_id,
                }
                for child_id, scenario_id in zip(child_ids, scenario_ids, strict=True)
            ],
        )

    return SuiteRunCreateResponse(parent_run_id=str(parent_id), call_count=len(scenario_ids))


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
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
) -> ScenarioSummary:
    async with engine.connect() as conn, conn.begin():
        suite_row = (
            (
                await conn.execute(
                    text("SELECT project_id FROM suites WHERE id = :id"), {"id": suite_id}
                )
            )
            .mappings()
            .first()
        )
        if suite_row is None:
            raise APIError("not_found", "suite not found", status.HTTP_404_NOT_FOUND)
        ensure_project_match(suite_row["project_id"], project_id)

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


async def _fetch_scenario_project_or_404(
    conn: AsyncConnection, scenario_id: uuid.UUID
) -> uuid.UUID:
    """Scenarios carry no project_id of their own (B2.5-01: only
    agents/suites/runs got the NOT NULL column) -- they scope transitively
    through their suite, same as run_events/turns scope through run_id."""
    row = (
        (
            await conn.execute(
                text(
                    "SELECT s.project_id FROM scenarios sc "
                    "JOIN suites s ON s.id = sc.suite_id WHERE sc.id = :id"
                ),
                {"id": scenario_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise APIError("not_found", "scenario not found", status.HTTP_404_NOT_FOUND)
    return uuid.UUID(str(row["project_id"]))


@router.patch("/v1/scenarios/{scenario_id}", response_model=ScenarioSummary)
async def update_scenario(
    scenario_id: uuid.UUID,
    body: ScenarioUpdate,
    user_id: str = Depends(require_user_id),
    project_id: uuid.UUID = Depends(require_project_id),
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
        scenario_project_id = await _fetch_scenario_project_or_404(conn, scenario_id)
        ensure_project_match(scenario_project_id, project_id)
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
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
) -> None:
    async with engine.connect() as conn, conn.begin():
        scenario_project_id = await _fetch_scenario_project_or_404(conn, scenario_id)
        ensure_project_match(scenario_project_id, project_id)
        result = await conn.execute(
            text("DELETE FROM scenarios WHERE id = :id"), {"id": scenario_id}
        )
        if result.rowcount == 0:
            raise APIError("not_found", "scenario not found", status.HTTP_404_NOT_FOUND)
