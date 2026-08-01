"""B1-08: idempotent seed data for fresh environments (2 agents, 2 suites,
6 scenarios, built-in personas, 4 completed FakeRunner runs) so the
dashboard and results screens aren't empty against a brand-new database.

Every row uses a fixed, deterministic id (`uuid5` of a stable key) and is
inserted with `ON CONFLICT (id) DO NOTHING`, so re-running this script never
creates duplicates -- the ticket's own "done when". Runs are the one
exception that needs an explicit existence check before acting: FakeRunner
itself isn't idempotent (it unconditionally flips status to 'running' and
inserts fresh turns/assertion_results rows), so re-running it against an
already-completed run id would flip status backwards and violate the turns
primary key on a second seed invocation.
"""

import asyncio
import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.db import get_engine
from app.workers.claim import ClaimedRun
from app.workers.fake_runner import run_fake_script

_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "cadence.seed")
_SEED_USER_ID = "seed"


def _id(key: str) -> uuid.UUID:
    return uuid.uuid5(_NAMESPACE, key)


_AGENTS: list[dict[str, Any]] = [
    {
        "key": "agent:support",
        "name": "Support Line Agent",
        "transport": "web",
        "max_concurrency": 3,
    },
    {
        "key": "agent:billing",
        "name": "Billing Agent",
        "transport": "web",
        "max_concurrency": 3,
    },
]

_PERSONAS: list[dict[str, Any]] = [
    {
        "key": "persona:priya",
        "name": "Priya Sharma",
        "voice": "alloy",
        "language": "en",
        "accent": "Indian English",
        "traits": {"tone": "polite", "patience": "medium"},
    },
    {
        "key": "persona:frank",
        "name": "Frustrated Frank",
        "voice": "verse",
        "language": "en",
        "accent": "American English",
        "traits": {"tone": "impatient", "patience": "low"},
    },
    {
        "key": "persona:elena",
        "name": "Elderly Elena",
        "voice": "shimmer",
        "language": "en",
        "accent": "British English",
        "traits": {"tone": "confused", "patience": "high"},
    },
]

_SUITES: list[dict[str, Any]] = [
    {
        "key": "suite:support",
        "agent_key": "agent:support",
        "name": "Card & Account Support",
        "description": "Core support flows: card block, balance, account questions.",
    },
    {
        "key": "suite:billing",
        "agent_key": "agent:billing",
        "name": "Billing & Payments",
        "description": "Billing disputes, payment plans, refund requests.",
    },
]

_SCENARIOS: list[dict[str, Any]] = [
    {
        "key": "scenario:support:card-block",
        "suite_key": "suite:support",
        "name": "Lost card, request block",
        "persona": "Priya Sharma",
        "persona_initials": "PS",
    },
    {
        "key": "scenario:support:balance",
        "suite_key": "suite:support",
        "name": "Balance inquiry",
        "persona": "Frustrated Frank",
        "persona_initials": "FF",
    },
    {
        "key": "scenario:support:account-update",
        "suite_key": "suite:support",
        "name": "Update contact details",
        "persona": "Elderly Elena",
        "persona_initials": "EE",
    },
    {
        "key": "scenario:billing:dispute",
        "suite_key": "suite:billing",
        "name": "Dispute a charge",
        "persona": "Priya Sharma",
        "persona_initials": "PS",
    },
    {
        "key": "scenario:billing:payment-plan",
        "suite_key": "suite:billing",
        "name": "Set up a payment plan",
        "persona": "Frustrated Frank",
        "persona_initials": "FF",
    },
    {
        "key": "scenario:billing:refund",
        "suite_key": "suite:billing",
        "name": "Request a refund",
        "persona": "Elderly Elena",
        "persona_initials": "EE",
    },
]

_SEED_ASSERTIONS = [
    {"id": "a1", "name": "Requests identity verification before action"},
    {"id": "a2", "name": "Confirms next steps before ending the call"},
]

# 4 of the 6 scenarios get a completed run seeded, 2 per suite.
_SEEDED_RUNS = [
    {"key": "run:support:card-block", "scenario_key": "scenario:support:card-block"},
    {"key": "run:support:balance", "scenario_key": "scenario:support:balance"},
    {"key": "run:billing:dispute", "scenario_key": "scenario:billing:dispute"},
    {"key": "run:billing:payment-plan", "scenario_key": "scenario:billing:payment-plan"},
]

_SCENARIO_TO_SUITE = {sc["key"]: sc["suite_key"] for sc in _SCENARIOS}
_SUITE_TO_AGENT = {s["key"]: s["agent_key"] for s in _SUITES}


async def _seed_agents(conn: AsyncConnection) -> dict[str, uuid.UUID]:
    ids: dict[str, uuid.UUID] = {}
    for a in _AGENTS:
        agent_id = _id(a["key"])
        ids[a["key"]] = agent_id
        await conn.execute(
            text(
                "INSERT INTO agents (id, name, transport, max_concurrency, created_by_user_id) "
                "VALUES (:id, :name, :transport, :max_concurrency, :user_id) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {
                "id": agent_id,
                "name": a["name"],
                "transport": a["transport"],
                "max_concurrency": a["max_concurrency"],
                "user_id": _SEED_USER_ID,
            },
        )
    return ids


async def _seed_personas(conn: AsyncConnection) -> None:
    for p in _PERSONAS:
        await conn.execute(
            text(
                "INSERT INTO personas (id, name, voice, language, accent, traits, builtin) "
                "VALUES (:id, :name, :voice, :language, :accent, CAST(:traits AS jsonb), true) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {
                "id": _id(p["key"]),
                "name": p["name"],
                "voice": p["voice"],
                "language": p["language"],
                "accent": p["accent"],
                "traits": json.dumps(p["traits"]),
            },
        )


async def _seed_suites(
    conn: AsyncConnection, agent_ids: dict[str, uuid.UUID]
) -> dict[str, uuid.UUID]:
    ids: dict[str, uuid.UUID] = {}
    for s in _SUITES:
        suite_id = _id(s["key"])
        ids[s["key"]] = suite_id
        await conn.execute(
            text(
                "INSERT INTO suites (id, name, description, agent_id, created_by_user_id) "
                "VALUES (:id, :name, :description, :agent_id, :user_id) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {
                "id": suite_id,
                "name": s["name"],
                "description": s["description"],
                "agent_id": agent_ids[s["agent_key"]],
                "user_id": _SEED_USER_ID,
            },
        )
    return ids


async def _seed_scenarios(
    conn: AsyncConnection, suite_ids: dict[str, uuid.UUID]
) -> dict[str, uuid.UUID]:
    ids: dict[str, uuid.UUID] = {}
    for sc in _SCENARIOS:
        scenario_id = _id(sc["key"])
        ids[sc["key"]] = scenario_id
        await conn.execute(
            text(
                "INSERT INTO scenarios "
                "(id, suite_id, name, persona, persona_initials, assertions, source) "
                "VALUES (:id, :suite_id, :name, :persona, :persona_initials, "
                " CAST(:assertions AS jsonb), 'manual') "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {
                "id": scenario_id,
                "suite_id": suite_ids[sc["suite_key"]],
                "name": sc["name"],
                "persona": sc["persona"],
                "persona_initials": sc["persona_initials"],
                "assertions": json.dumps(_SEED_ASSERTIONS),
            },
        )
    return ids


async def _seed_run(
    engine: AsyncEngine,
    run: dict[str, str],
    agent_ids: dict[str, uuid.UUID],
    scenario_ids: dict[str, uuid.UUID],
) -> None:
    run_id = _id(run["key"])
    scenario_key = run["scenario_key"]
    scenario_id = scenario_ids[scenario_key]
    agent_id = agent_ids[_SUITE_TO_AGENT[_SCENARIO_TO_SUITE[scenario_key]]]

    async with engine.connect() as conn:
        already_seeded = (
            await conn.execute(text("SELECT 1 FROM runs WHERE id = :id"), {"id": run_id})
        ).first()
    if already_seeded is not None:
        return

    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO runs (id, type, agent_id, scenario_id, created_by_user_id) "
                "VALUES (:id, 'simulation', :agent_id, :scenario_id, :user_id)"
            ),
            {
                "id": run_id,
                "agent_id": agent_id,
                "scenario_id": scenario_id,
                "user_id": _SEED_USER_ID,
            },
        )

    # Bypasses claim_run's global-oldest-queued claim on purpose -- the run
    # id is already known and fixed, so there's nothing to race with, and
    # run_fake_script's first action unconditionally sets status='running'
    # regardless of whether it arrived via 'queued' or 'claimed'.
    claimed = ClaimedRun(
        id=run_id, type="simulation", agent_id=agent_id, scenario_id=scenario_id, config={}
    )
    await run_fake_script(engine, claimed)


async def seed(engine: AsyncEngine) -> None:
    async with engine.connect() as conn, conn.begin():
        agent_ids = await _seed_agents(conn)
        await _seed_personas(conn)
        suite_ids = await _seed_suites(conn, agent_ids)
        scenario_ids = await _seed_scenarios(conn, suite_ids)

    for run in _SEEDED_RUNS:
        await _seed_run(engine, run, agent_ids, scenario_ids)


async def main() -> None:
    await seed(get_engine())
    print("Seed complete: 2 agents, 2 suites, 6 scenarios, 3 personas, 4 completed runs.")


if __name__ == "__main__":
    asyncio.run(main())
