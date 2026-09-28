import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, Response, status
from sqlalchemy import bindparam, text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.config import get_settings
from app.crypto import decrypt_json, encrypt_json
from app.db import get_engine
from app.deps import ensure_project_match, require_project_id, require_user_id
from app.engine.generation.generator import generate_drafts
from app.engine.judge.judge import GenAIClient, build_judge_client
from app.engine.metrics.resolver import resolve_metrics_for_agent
from app.errors import APIError
from app.formatting import relative_time
from app.schemas.suites import (
    GeneratedDraftSummary,
    ScenarioCreateRequest,
    ScenarioDuplicateRequest,
    ScenarioGenerateRequest,
    ScenarioGenerateResponse,
    ScenarioMetricAttachment,
    ScenarioSummary,
    ScenarioUpdate,
    SuiteCreate,
    SuiteDetail,
    SuiteListItem,
    SuiteMoveRequest,
    SuiteRunCreate,
    SuiteRunCreateResponse,
    SuiteUpdate,
)
from app.usage import UsageTracker
from app.verdict import Verdict, format_score, verdict_for_run

router = APIRouter(tags=["suites"])


def get_generation_client_factory() -> Callable[[], GenAIClient]:
    """Returns a *factory*, not a built client. FastAPI's solve_dependencies
    calls every Depends() for a request in one pass, including this one,
    regardless of whether a sibling parameter (like the request body) ends
    up failing validation -- confirmed empirically: an out-of-range `count`
    still reached this function before the 422 was raised. Eagerly calling
    `build_judge_client()` here (real GCP credential loading) crashed that
    case with a raw KeyError instead of a clean 422 in any environment
    without credentials configured -- the exact class of bug B2.7-10
    shipped once already. Returning the uncalled factory instead defers
    real construction to generate_scenarios's own body, which only runs
    once FastAPI has confirmed the body actually validated. Tests override
    this dependency with a fake factory (`lambda: fake_client`), same
    testability B2.7-10's `build_judge_client` also gives directly."""
    return build_judge_client


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


def _dump_json(value: object) -> str:
    return json.dumps(value)


def _assert_count(assertions: object) -> int:
    return len(assertions) if isinstance(assertions, list) else 0


def _scenario_summary(
    row: RowMapping | Mapping[str, Any], latest: _LatestRun | None, assert_count: int
) -> ScenarioSummary:
    verdict, score = _verdict_and_score(latest)
    return ScenarioSummary(
        id=str(row["id"]),
        suite_id=str(row["suite_id"]),
        name=row["name"],
        persona=row["persona_name"] or "",
        persona_id=str(row["persona_id"]) if row["persona_id"] else None,
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
        folder=suite_row["folder"],
        rubric=dict(suite_row["rubric"]) if suite_row["rubric"] else None,
    )


_SUITES_SQL = text(
    "SELECT s.id, s.project_id, s.name, s.description, s.folder, s.rubric, "
    "       a.name AS agent_name "
    "FROM suites s JOIN agents a ON a.id = s.agent_id "
    "WHERE s.project_id = :project_id "
    "ORDER BY s.created_at"
)
_SCENARIOS_FOR_PROJECT_SQL = text(
    "SELECT sc.id, sc.suite_id, sc.name, sc.persona_id, p.name AS persona_name, sc.assertions "
    "FROM scenarios sc "
    "JOIN suites s ON s.id = sc.suite_id "
    "LEFT JOIN personas p ON p.id = sc.persona_id "
    "WHERE s.project_id = :project_id ORDER BY sc.suite_id, sc.created_at"
)
_SUITE_BY_ID_SQL = text(
    "SELECT s.id, s.project_id, s.name, s.description, s.folder, s.rubric, "
    "       a.name AS agent_name "
    "FROM suites s JOIN agents a ON a.id = s.agent_id WHERE s.id = :id"
)
_SCENARIOS_FOR_SUITE_SQL = text(
    "SELECT sc.id, sc.suite_id, sc.name, sc.persona_id, p.name AS persona_name, sc.assertions "
    "FROM scenarios sc LEFT JOIN personas p ON p.id = sc.persona_id "
    "WHERE sc.suite_id = :suite_id ORDER BY sc.created_at"
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
                        "(id, project_id, name, description, agent_id, folder, rubric, "
                        " created_by_user_id) "
                        "VALUES (:id, :project_id, :name, :description, :agent_id, :folder, "
                        " CAST(:rubric AS jsonb), :user_id) "
                        "RETURNING id, project_id, name, description, folder, rubric"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "project_id": project_id,
                        "name": body.name,
                        "description": body.description,
                        "agent_id": agent_id,
                        "folder": body.folder,
                        "rubric": json.dumps(body.rubric) if body.rubric is not None else None,
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
        folder=row["folder"],
        rubric=dict(row["rubric"]) if row["rubric"] else None,
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
    if body.folder is not None:
        updates["folder"] = body.folder
    if body.rubric is not None:
        updates["rubric"] = body.rubric

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
            assignments = []
            params: dict[str, Any] = {"id": suite_id}
            for col, val in updates.items():
                if col == "rubric":
                    assignments.append(f"{col} = CAST(:{col} AS jsonb)")
                    params[col] = json.dumps(val)
                else:
                    assignments.append(f"{col} = :{col}")
                    params[col] = val
            await conn.execute(
                text(f"UPDATE suites SET {', '.join(assignments)} WHERE id = :id"), params
            )
    return await get_suite(suite_id, project_id, engine)


@router.post("/v1/suites/move", response_model=list[str])
async def move_suites(
    body: SuiteMoveRequest,
    user_id: str = Depends(require_user_id),
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
) -> list[str]:
    """B2.7-11 bulk op: reassign `folder` on many suites at once. Every
    suite id must belong to the caller's project -- silently no-oping on a
    foreign suite id would look like success from the UI's side while
    actually moving nothing."""
    suite_ids = [_parse_uuid(sid, "suiteIds") for sid in body.suite_ids]
    if not suite_ids:
        return []
    async with engine.connect() as conn, conn.begin():
        owned = (
            (
                await conn.execute(
                    text("SELECT id FROM suites WHERE id = ANY(:ids) AND project_id = :project_id"),
                    {"ids": suite_ids, "project_id": project_id},
                )
            )
            .scalars()
            .all()
        )
        if len(owned) != len(suite_ids):
            raise APIError("not_found", "one or more suites not found", status.HTTP_404_NOT_FOUND)
        await conn.execute(
            text("UPDATE suites SET folder = :folder WHERE id = ANY(:ids)"),
            {"folder": body.folder, "ids": suite_ids},
        )
    return [str(sid) for sid in suite_ids]


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
    " created_by_user_id, trigger) "
    "VALUES (:id, :project_id, 'suite', :agent_id, CAST(:config AS jsonb), :idempotency_key, "
    " :user_id, :trigger) "
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
# candidate scan (B3-03: "childless" claimability). trigger is stamped
# with the SAME value as the parent (B2.6-03: "children inherit") -- not
# left to its own server_default, which would silently disagree with the
# parent the moment a non-'manual' caller (E3's GitHub Action, a future
# schedule) exists.
_INSERT_CHILD_RUN_SQL = text(
    "INSERT INTO runs (id, project_id, type, agent_id, scenario_id, parent_run_id, config, "
    " created_by_user_id, trigger) "
    "VALUES (:id, :project_id, 'simulation', :agent_id, :scenario_id, :parent_run_id, "
    " CAST(:config AS jsonb), :user_id, :trigger)"
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
                (await conn.execute(_ALL_SCENARIO_IDS_FOR_SUITE_SQL, {"suite_id": suite_id}))
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
                f"this run would queue {len(scenario_ids)} calls, over the cap of {cap}",
                status.HTTP_422_UNPROCESSABLE_CONTENT,
            )

        # B2.6-03: no caller passes anything but 'manual' yet (same as
        # app.api.runs._create_run's default) -- named here rather than
        # inlined twice below so the parent and every child are provably
        # stamped with the same value ("children inherit").
        trigger = "manual"

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
                        "trigger": trigger,
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
                    "trigger": trigger,
                }
                for child_id, scenario_id in zip(child_ids, scenario_ids, strict=True)
            ],
        )

    return SuiteRunCreateResponse(parent_run_id=str(parent_id), call_count=len(scenario_ids))


async def _resolve_persona(
    conn: AsyncConnection,
    project_id: uuid.UUID,
    persona_id: str | None,
    suggested_name: str | None,
) -> tuple[uuid.UUID, str]:
    """Every scenario needs a real `personas` row now (B2.7-11). An explicit
    `personaId` is validated for visibility (builtin, or this project's
    own); with none given (the from-draft path, where the draft only ever
    carried a free-text suggestion), fall back to an exact case-insensitive
    name match, scoped the same way, and 422 rather than silently picking
    an unrelated project's same-named persona."""
    if persona_id is not None:
        pid = _parse_uuid(persona_id, "personaId")
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT id, name FROM personas "
                        "WHERE id = :id AND (project_id IS NULL OR project_id = :project_id)"
                    ),
                    {"id": pid, "project_id": project_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise APIError("not_found", "persona not found", status.HTTP_404_NOT_FOUND)
        return uuid.UUID(str(row["id"])), row["name"]

    row = (
        (
            await conn.execute(
                text(
                    "SELECT id, name FROM personas WHERE lower(name) = lower(:name) "
                    "AND (project_id IS NULL OR project_id = :project_id) LIMIT 1"
                ),
                {"name": suggested_name, "project_id": project_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise APIError(
            "persona_not_found",
            f"no persona named {suggested_name!r} found in this project or the built-ins -- "
            "pass personaId explicitly",
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    return uuid.UUID(str(row["id"])), row["name"]


async def _sync_scenario_metrics(
    conn: AsyncConnection,
    scenario_id: uuid.UUID,
    project_id: uuid.UUID,
    metrics: list[ScenarioMetricAttachment],
) -> None:
    """Replaces the full attachment set -- an empty list clears it, same
    "omitted vs. provided" convention as ScenarioUpdate.metrics itself.
    Each metric_id is checked for project visibility first so this can
    never leave a scenario_metrics row pointing at another project's
    metric (each metric row is validated individually, not batched,
    since a single bad id shouldn't silently drop the rest)."""
    for m in metrics:
        mid = _parse_uuid(m.metric_id, "metrics[].metricId")
        exists = (
            await conn.execute(
                text(
                    "SELECT 1 FROM metrics "
                    "WHERE id = :id AND (project_id IS NULL OR project_id = :project_id)"
                ),
                {"id": mid, "project_id": project_id},
            )
        ).first()
        if exists is None:
            raise APIError(
                "not_found", f"metric {m.metric_id} not found", status.HTTP_404_NOT_FOUND
            )
    await conn.execute(
        text("DELETE FROM scenario_metrics WHERE scenario_id = :id"), {"id": scenario_id}
    )
    for m in metrics:
        await conn.execute(
            text(
                "INSERT INTO scenario_metrics (scenario_id, metric_id, gating) "
                "VALUES (:sid, :mid, :gating)"
            ),
            {"sid": scenario_id, "mid": uuid.UUID(m.metric_id), "gating": m.gating},
        )


_AGENT_FOR_GENERATION_SQL = text(
    "SELECT id, project_id, prompt, description FROM agents WHERE id = :id"
)

_DISCOVERY_RUN_SQL = text("SELECT id, project_id, type, status FROM runs WHERE id = :id")

_DISCOVERY_INTENTS_SQL = text(
    "SELECT name, path, state, reason FROM discovery_intents WHERE run_id = :run_id"
)

_VISIBLE_PERSONAS_FOR_GENERATION_SQL = text(
    "SELECT id, name FROM personas "
    "WHERE project_id IS NULL OR project_id = :project_id "
    "ORDER BY builtin DESC, name"
)


async def _generation_context(
    conn: AsyncConnection,
    *,
    body: ScenarioGenerateRequest,
    agent_row: RowMapping,
    project_id: uuid.UUID,
) -> tuple[str, str, uuid.UUID | None]:
    """Returns (source_label, context_text, run_id_to_store)."""
    if body.source == "agent_prompt":
        if not agent_row["prompt"]:
            raise APIError(
                "validation_error",
                "this agent has no prompt authored yet -- add one before generating from it",
                status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
        return "voice agent's system prompt", agent_row["prompt"], None

    if body.source == "agent_description":
        if not agent_row["description"]:
            raise APIError(
                "validation_error",
                "this agent has no description authored yet -- add one before generating from it",
                status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
        return "voice agent's description", agent_row["description"], None

    assert body.discovery_run_id is not None  # enforced by the request schema
    run_id = _parse_uuid(body.discovery_run_id, "discoveryRunId")
    run_row = (await conn.execute(_DISCOVERY_RUN_SQL, {"id": run_id})).mappings().first()
    if run_row is None or uuid.UUID(str(run_row["project_id"])) != project_id:
        raise APIError("not_found", "discovery run not found", status.HTTP_404_NOT_FOUND)
    if run_row["type"] != "discovery" or run_row["status"] != "completed":
        raise APIError(
            "validation_error",
            "discoveryRunId must name a completed discovery run",
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    intents = (await conn.execute(_DISCOVERY_INTENTS_SQL, {"run_id": run_id})).mappings().all()
    lines = [
        f"- {i['name']} ({i['path']}): {i['state']}" + (f" -- {i['reason']}" if i["reason"] else "")
        for i in intents
    ]
    context_text = "\n".join(lines) if lines else "(no intents recorded for this discovery run)"
    return "completed discovery run's explored intents", context_text, run_id


@router.post(
    "/v1/suites/{suite_id}/scenarios:generate",
    response_model=ScenarioGenerateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def generate_scenarios(
    suite_id: uuid.UUID,
    body: ScenarioGenerateRequest,
    user_id: str = Depends(require_user_id),
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
    client_factory: Callable[[], GenAIClient] = Depends(get_generation_client_factory),
) -> ScenarioGenerateResponse:
    """B2.7-12. Writes `discovery_drafts` rows only -- acceptance goes
    through `add_scenario`'s existing `fromDraftId` path (idempotent via
    `source_draft_ref` UNIQUE), reusing tested machinery rather than a
    second accept path."""
    async with engine.connect() as conn:
        suite_row = (
            (
                await conn.execute(
                    text("SELECT project_id, agent_id FROM suites WHERE id = :id"), {"id": suite_id}
                )
            )
            .mappings()
            .first()
        )
        if suite_row is None:
            raise APIError("not_found", "suite not found", status.HTTP_404_NOT_FOUND)
        ensure_project_match(suite_row["project_id"], project_id)
        agent_id = uuid.UUID(str(suite_row["agent_id"]))

        agent_row = (
            (await conn.execute(_AGENT_FOR_GENERATION_SQL, {"id": agent_id})).mappings().first()
        )
        assert agent_row is not None  # suites.agent_id is NOT NULL + FK

        source_label, context_text, run_id = await _generation_context(
            conn, body=body, agent_row=agent_row, project_id=project_id
        )

        persona_rows = (
            (await conn.execute(_VISIBLE_PERSONAS_FOR_GENERATION_SQL, {"project_id": project_id}))
            .mappings()
            .all()
        )
        if not persona_rows:
            raise APIError(
                "validation_error",
                "no personas are visible to this project -- create one before generating",
                status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
        metric_rows = await resolve_metrics_for_agent(
            conn, project_id=project_id, agent_id=agent_id
        )

    persona_by_name = {str(p["name"]).lower(): p for p in persona_rows}
    metric_by_name = {str(m["name"]).lower(): m for m in metric_rows}
    fallback_persona = persona_rows[0]

    # Real credential loading happens here, not in the Depends() above --
    # see get_generation_client_factory's docstring for why.
    client = client_factory()
    usage = UsageTracker()
    result = await generate_drafts(
        client,
        source_label=source_label,
        context_text=context_text,
        persona_names=[p["name"] for p in persona_rows],
        metric_names=[m["name"] for m in metric_rows],
        count=body.count,
        usage=usage,
    )
    # Enforce the negative/refusal case rather than trust the model to
    # have honored the instruction -- retry once, then fail loudly (never
    # silently ship a batch missing the case that matters most).
    if not any(d.kind == "negative" for d in result.drafts):
        result = await generate_drafts(
            client,
            source_label=source_label,
            context_text=context_text,
            persona_names=[p["name"] for p in persona_rows],
            metric_names=[m["name"] for m in metric_rows],
            count=body.count,
            usage=usage,
        )
        if not any(d.kind == "negative" for d in result.drafts):
            raise APIError(
                "generation_failed",
                "generation did not produce a negative/refusal case after a retry",
                status.HTTP_502_BAD_GATEWAY,
            )

    drafts_out: list[GeneratedDraftSummary] = []
    async with engine.connect() as conn, conn.begin():
        for draft in result.drafts:
            persona_row = persona_by_name.get(draft.persona_name.lower(), fallback_persona)
            metric_ids = [
                str(metric_by_name[name.lower()]["id"])
                for name in draft.metric_names
                if name.lower() in metric_by_name
            ]
            seen_ids: set[str] = set()
            assertions: list[object] = []
            for a in draft.assertions:
                if a.id in seen_ids:
                    continue
                seen_ids.add(a.id)
                assertions.append(
                    {
                        "id": a.id,
                        "name": a.name,
                        "description": a.description,
                        "distinguishFrom": a.distinguish_from,
                    }
                )

            draft_id = str(uuid.uuid4())
            script = {"goal": draft.caller_goal, "openingLine": draft.opening_line}
            proposed_metrics = [{"metricId": mid} for mid in metric_ids]
            await conn.execute(
                text(
                    "INSERT INTO discovery_drafts "
                    "(draft_id, run_id, suite_id, source, name, persona, persona_id, goal, "
                    " script, assertions, proposed_metrics) "
                    "VALUES (:draft_id, :run_id, :suite_id, :source, :name, :persona, "
                    " :persona_id, :goal, CAST(:script AS jsonb), CAST(:assertions AS jsonb), "
                    " CAST(:proposed_metrics AS jsonb))"
                ),
                {
                    "draft_id": draft_id,
                    "run_id": run_id,
                    "suite_id": suite_id,
                    "source": body.source,
                    "name": draft.name,
                    "persona": draft.persona_name,
                    "persona_id": persona_row["id"],
                    "goal": draft.scenario_goal,
                    "script": _dump_json(script),
                    "assertions": _dump_json(assertions),
                    "proposed_metrics": _dump_json(proposed_metrics),
                },
            )
            drafts_out.append(
                GeneratedDraftSummary(
                    draft_id=draft_id,
                    name=draft.name,
                    kind=draft.kind,
                    persona=str(persona_row["name"]),
                    persona_id=str(persona_row["id"]),
                    goal=draft.scenario_goal,
                    assertions=assertions,
                    metric_names=[
                        name for name in draft.metric_names if name.lower() in metric_by_name
                    ],
                )
            )

    return ScenarioGenerateResponse(drafts=drafts_out, cost=usage.as_dict())


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
                            "SELECT sc.id, sc.suite_id, sc.name, sc.persona_id, "
                            "       p.name AS persona_name, sc.assertions "
                            "FROM scenarios sc LEFT JOIN personas p ON p.id = sc.persona_id "
                            "WHERE sc.source_draft_ref = :ref"
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

            # draft_id alone is the PK as of B2.7-12 (migration 017) -- this
            # lookup no longer needs a run_id to scope by; project scoping
            # instead comes from the draft's suite (joined below), so a
            # draft can't be accepted into a different project than the one
            # that generated it.
            draft = (
                (
                    await conn.execute(
                        text(
                            "SELECT dd.name, dd.persona, dd.persona_id, dd.goal, dd.script, "
                            "       dd.assertions, dd.proposed_metrics, s.project_id "
                            "FROM discovery_drafts dd JOIN suites s ON s.id = dd.suite_id "
                            "WHERE dd.draft_id = :draft_id"
                        ),
                        {"draft_id": body.from_draft_id},
                    )
                )
                .mappings()
                .first()
            )
            if draft is None or uuid.UUID(str(draft["project_id"])) != project_id:
                raise APIError("not_found", "draft not found", status.HTTP_404_NOT_FOUND)
            name, assertions = draft["name"], draft["assertions"]
            source, source_draft_ref = "discovery_draft", body.from_draft_id
            # Body fields take precedence over the draft's own proposal --
            # a draft is a suggestion, and a caller editing it before
            # accepting shouldn't have those edits silently overwritten.
            script = body.script if body.script is not None else draft["script"]
            goal = body.goal if body.goal is not None else draft["goal"]
            if body.persona_id is not None:
                persona_id, persona_name = await _resolve_persona(
                    conn, project_id, body.persona_id, None
                )
            elif draft["persona_id"] is not None:
                persona_id, persona_name = await _resolve_persona(
                    conn, project_id, str(draft["persona_id"]), None
                )
            else:
                persona_id, persona_name = await _resolve_persona(
                    conn, project_id, None, draft["persona"]
                )
            metrics_to_attach = body.metrics or [
                ScenarioMetricAttachment(metric_id=m["metricId"])
                for m in (draft["proposed_metrics"] or [])
            ]
        else:
            assert body.name is not None
            assert body.persona_id is not None
            name, assertions = body.name, body.assertions
            source, source_draft_ref = "manual", None
            script = body.script
            goal = body.goal
            persona_id, persona_name = await _resolve_persona(
                conn, project_id, body.persona_id, None
            )
            metrics_to_attach = body.metrics

        row = (
            (
                await conn.execute(
                    text(
                        "INSERT INTO scenarios "
                        "(id, suite_id, name, persona_id, script, assertions, goal, "
                        " test_profile_id, conditions, source, source_draft_ref) "
                        "VALUES (:id, :suite_id, :name, :persona_id, CAST(:script AS jsonb), "
                        " CAST(:assertions AS jsonb), :goal, :test_profile_id, "
                        " CAST(:conditions AS jsonb), :source, :source_draft_ref) "
                        "RETURNING id, suite_id, name, assertions"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "suite_id": suite_id,
                        "name": name,
                        "persona_id": persona_id,
                        "script": json.dumps(script) if script is not None else None,
                        "assertions": json.dumps(assertions),
                        "goal": goal,
                        "test_profile_id": (
                            _parse_uuid(body.test_profile_id, "testProfileId")
                            if body.test_profile_id
                            else None
                        ),
                        "conditions": (
                            json.dumps(body.conditions) if body.conditions is not None else None
                        ),
                        "source": source,
                        "source_draft_ref": source_draft_ref,
                    },
                )
            )
            .mappings()
            .one()
        )
        if metrics_to_attach:
            await _sync_scenario_metrics(
                conn, uuid.UUID(str(row["id"])), project_id, metrics_to_attach
            )
        if source == "discovery_draft":
            await conn.execute(
                text(
                    "UPDATE discovery_drafts SET added_scenario_id = :scenario_id "
                    "WHERE draft_id = :draft_id"
                ),
                {"scenario_id": row["id"], "draft_id": source_draft_ref},
            )
    result_row = {**row, "persona_id": persona_id, "persona_name": persona_name}
    return _scenario_summary(result_row, None, _assert_count(row["assertions"]))


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
    if body.script is not None:
        updates["script"] = body.script
    if body.assertions is not None:
        updates["assertions"] = json.dumps(body.assertions)
    if body.goal is not None:
        updates["goal"] = body.goal
    if body.test_profile_id is not None:
        updates["test_profile_id"] = _parse_uuid(body.test_profile_id, "testProfileId")
    if body.conditions is not None:
        updates["conditions"] = json.dumps(body.conditions)

    async with engine.connect() as conn, conn.begin():
        scenario_project_id = await _fetch_scenario_project_or_404(conn, scenario_id)
        ensure_project_match(scenario_project_id, project_id)
        if body.persona_id is not None:
            persona_id, _persona_name = await _resolve_persona(
                conn, project_id, body.persona_id, None
            )
            updates["persona_id"] = persona_id
        if updates:
            assignments = []
            params: dict[str, Any] = {"id": scenario_id}
            for col, val in updates.items():
                if col in ("assertions", "conditions"):
                    assignments.append(f"{col} = CAST(:{col} AS jsonb)")
                else:
                    assignments.append(f"{col} = :{col}")
                params[col] = val
            await conn.execute(
                text(f"UPDATE scenarios SET {', '.join(assignments)} WHERE id = :id"), params
            )
        if body.metrics is not None:
            await _sync_scenario_metrics(conn, scenario_id, project_id, body.metrics)
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT sc.id, sc.suite_id, sc.name, sc.persona_id, "
                        "       p.name AS persona_name, sc.assertions "
                        "FROM scenarios sc LEFT JOIN personas p ON p.id = sc.persona_id "
                        "WHERE sc.id = :id"
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


async def _copy_persona_for_project(
    conn: AsyncConnection, persona_id: uuid.UUID, target_project_id: uuid.UUID
) -> uuid.UUID:
    """A builtin persona (project_id IS NULL) is already visible from any
    project -- reused as-is, same as a same-project duplicate. Only a
    project-owned persona needs an actual copy, mirroring
    POST /v1/personas/{id}/duplicate's own INSERT shape."""
    row = (
        (
            await conn.execute(
                text(
                    "SELECT project_id, name, voice, language, accent, traits, emotion, "
                    "speaking_rate, interruption_behavior, environment, code_switch "
                    "FROM personas WHERE id = :id"
                ),
                {"id": persona_id},
            )
        )
        .mappings()
        .one()
    )
    if row["project_id"] is None or uuid.UUID(str(row["project_id"])) == target_project_id:
        return persona_id
    new_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO personas "
            "(id, project_id, name, voice, language, accent, traits, builtin, emotion, "
            " speaking_rate, interruption_behavior, environment, code_switch) "
            "VALUES (:id, :project_id, :name, :voice, :language, :accent, "
            " CAST(:traits AS jsonb), false, :emotion, :speaking_rate, "
            " :interruption_behavior, :environment, CAST(:code_switch AS jsonb))"
        ),
        {
            "id": new_id,
            "project_id": target_project_id,
            "name": row["name"],
            "voice": row["voice"],
            "language": row["language"],
            "accent": row["accent"],
            "traits": _dump_json(row["traits"]),
            "emotion": row["emotion"],
            "speaking_rate": row["speaking_rate"],
            "interruption_behavior": row["interruption_behavior"],
            "environment": row["environment"],
            "code_switch": _dump_json(row["code_switch"]) if row["code_switch"] else None,
        },
    )
    return new_id


async def _copy_test_profile_for_project(
    conn: AsyncConnection, user_id: str, profile_id: uuid.UUID, target_project_id: uuid.UUID
) -> uuid.UUID:
    """Test profiles are always project-scoped (no builtin concept), so a
    cross-project duplicate always needs a real copy -- decrypting the
    source (access-logged, same as a direct GET) and re-encrypting into a
    fresh row rather than copying ciphertext directly, since app.crypto's
    envelope isn't guaranteed portable across an unrelated write path."""
    row = (
        (
            await conn.execute(
                text("SELECT project_id, name, fields FROM test_profiles WHERE id = :id"),
                {"id": profile_id},
            )
        )
        .mappings()
        .one()
    )
    if uuid.UUID(str(row["project_id"])) == target_project_id:
        return profile_id
    decrypted = decrypt_json(row["fields"])
    await conn.execute(
        text(
            "INSERT INTO sensitive_access_log (id, resource_type, resource_id, user_id, action) "
            "VALUES (:id, 'test_profile', :resource_id, :user_id, 'read')"
        ),
        {"id": uuid.uuid4(), "resource_id": profile_id, "user_id": user_id},
    )
    new_id = uuid.uuid4()
    envelope = encrypt_json(decrypted)
    await conn.execute(
        text(
            "INSERT INTO test_profiles (id, project_id, name, fields, encrypted, "
            " created_by_user_id) "
            "VALUES (:id, :project_id, :name, CAST(:fields AS jsonb), true, :user_id)"
        ),
        {
            "id": new_id,
            "project_id": target_project_id,
            "name": row["name"],
            "fields": _dump_json(envelope),
            "user_id": user_id,
        },
    )
    return new_id


@router.post(
    "/v1/scenarios/{scenario_id}/duplicate",
    response_model=ScenarioSummary,
    status_code=status.HTTP_201_CREATED,
)
async def duplicate_scenario(
    scenario_id: uuid.UUID,
    body: ScenarioDuplicateRequest,
    user_id: str = Depends(require_user_id),
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
) -> ScenarioSummary:
    """B2.7-11 bulk op. The target suite may belong to any project (this
    codebase has no per-user project-membership ACL anywhere else either --
    X-Project-Id is the only scoping this system has, and it only ever
    gates the *source* side of an action); a cross-project duplicate copies
    a project-owned persona/test-profile by value into the target project
    rather than leaving a reference that would dangle once the source
    project can no longer be assumed reachable. A scenario_metrics
    attachment whose metric isn't visible in the target project is dropped
    (not copied as a dangling reference) rather than erroring the whole
    duplicate."""
    target_suite_id = _parse_uuid(body.target_suite_id, "targetSuiteId")
    async with engine.connect() as conn, conn.begin():
        source_project_id = await _fetch_scenario_project_or_404(conn, scenario_id)
        ensure_project_match(source_project_id, project_id)
        source = (
            (
                await conn.execute(
                    text(
                        "SELECT name, persona_id, script, assertions, goal, test_profile_id, "
                        "conditions FROM scenarios WHERE id = :id"
                    ),
                    {"id": scenario_id},
                )
            )
            .mappings()
            .one()
        )

        target_suite = (
            (
                await conn.execute(
                    text("SELECT project_id FROM suites WHERE id = :id"), {"id": target_suite_id}
                )
            )
            .mappings()
            .first()
        )
        if target_suite is None:
            raise APIError("not_found", "target suite not found", status.HTTP_404_NOT_FOUND)
        target_project_id = uuid.UUID(str(target_suite["project_id"]))
        if (
            body.target_project_id is not None
            and _parse_uuid(body.target_project_id, "targetProjectId") != target_project_id
        ):
            raise APIError(
                "validation_error",
                "targetProjectId does not match targetSuiteId's own project",
                status.HTTP_422_UNPROCESSABLE_CONTENT,
            )

        new_persona_id = await _copy_persona_for_project(
            conn, uuid.UUID(str(source["persona_id"])), target_project_id
        )
        new_test_profile_id = (
            await _copy_test_profile_for_project(
                conn, user_id, uuid.UUID(str(source["test_profile_id"])), target_project_id
            )
            if source["test_profile_id"] is not None
            else None
        )

        new_id = uuid.uuid4()
        row = (
            (
                await conn.execute(
                    text(
                        "INSERT INTO scenarios "
                        "(id, suite_id, name, persona_id, script, assertions, goal, "
                        " test_profile_id, conditions, source) "
                        "VALUES (:id, :suite_id, :name, :persona_id, CAST(:script AS jsonb), "
                        " CAST(:assertions AS jsonb), :goal, :test_profile_id, "
                        " CAST(:conditions AS jsonb), 'manual') "
                        "RETURNING id, suite_id, name, assertions"
                    ),
                    {
                        "id": new_id,
                        "suite_id": target_suite_id,
                        "name": f"{source['name']} (copy)",
                        "persona_id": new_persona_id,
                        "script": _dump_json(source["script"]) if source["script"] else None,
                        "assertions": _dump_json(source["assertions"]),
                        "goal": source["goal"],
                        "test_profile_id": new_test_profile_id,
                        "conditions": (
                            _dump_json(source["conditions"]) if source["conditions"] else None
                        ),
                    },
                )
            )
            .mappings()
            .one()
        )

        source_metrics = (
            (
                await conn.execute(
                    text(
                        "SELECT sm.metric_id, sm.gating FROM scenario_metrics sm "
                        "JOIN metrics m ON m.id = sm.metric_id "
                        "WHERE sm.scenario_id = :id "
                        # Drop, don't dangle: only copy an attachment whose metric is
                        # actually visible (builtin or same-project) in the target.
                        "AND (m.project_id IS NULL OR m.project_id = :target_project_id)"
                    ),
                    {"id": scenario_id, "target_project_id": target_project_id},
                )
            )
            .mappings()
            .all()
        )
        if source_metrics:
            await conn.execute(
                text(
                    "INSERT INTO scenario_metrics (scenario_id, metric_id, gating) "
                    "SELECT :new_id, m.metric_id, m.gating "
                    "FROM (VALUES "
                    + ", ".join(
                        f"(CAST(:mid{i} AS uuid), CAST(:gating{i} AS boolean))"
                        for i in range(len(source_metrics))
                    )
                    + ") AS m(metric_id, gating)"
                ),
                {
                    "new_id": new_id,
                    **{f"mid{i}": m["metric_id"] for i, m in enumerate(source_metrics)},
                    **{f"gating{i}": m["gating"] for i, m in enumerate(source_metrics)},
                },
            )

        persona_row = (
            (
                await conn.execute(
                    text("SELECT name FROM personas WHERE id = :id"), {"id": new_persona_id}
                )
            )
            .mappings()
            .one()
        )

    result_row = {**row, "persona_id": new_persona_id, "persona_name": persona_row["name"]}
    return _scenario_summary(result_row, None, _assert_count(row["assertions"]))
