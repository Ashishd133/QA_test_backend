"""B1-08: idempotent seed data for fresh environments (3 agents, 3 suites,
7 scenarios, built-in personas, built-in metrics, 4 completed FakeRunner
runs) so the dashboard and results screens aren't empty against a brand-new
database.

Every row uses a fixed, deterministic id (`uuid5` of a stable key) and is
inserted with `ON CONFLICT (id) DO NOTHING`, so re-running this script never
creates duplicates -- the ticket's own "done when". Runs are the one
exception that needs an explicit existence check before acting: FakeRunner
itself isn't idempotent (it unconditionally flips status to 'running' and
inserts fresh turns/assertion_results rows), so re-running it against an
already-completed run id would flip status backwards and violate the turns
primary key on a second seed invocation.

B2-08: `agent:reference`/`suite:reference`/`scenario:reference:card-block`
are the one scenario a real executor can actually run -- everything else
here is dashboard-population filler with no agent behind it. Its
`assertions`/`script` carry full judge-ready content (description/
distinguishFrom, goal/openingLine/traits) reusing the exact hand-validated
text from `evals/assertions.py` and `CARD_BLOCK_PERSONA`
(app/engine/caller/persona_call.py), rather than the bare `{id, name}`
the other 6 scenarios use -- those were never meant to be executed for
real, so B2-05's `AssertionSpec.description` gap never mattered for them.
"""

import argparse
import asyncio
import json
import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.crypto import encrypt_text
from app.db import get_engine
from app.engine.caller.persona_call import CARD_BLOCK_PERSONA
from app.engine.metrics.builtins import BUILTIN_METRICS
from app.engine.reference_agent.agent import BANKER_INSTRUCTIONS_TEMPLATE, GREETER_INSTRUCTIONS
from app.workers.claim import ClaimedRun
from app.workers.fake_runner import run_fake_script
from evals.assertions import A1_REQUESTS_VERIFICATION, A2_CONFIRMS_NEXT_STEPS

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
    {
        "key": "agent:reference",
        # B2-08: the one agent a real executor run actually targets --
        # app.engine.reference_agent.agent, run as its own LiveKit worker
        # process. No per-agent connection config needed here (unlike
        # WebConfig's roomUrl/token): the executor creates a fresh room per
        # run and the reference agent's own worker auto-dispatches to it,
        # same as app.engine.caller.persona_call's validation flow.
        "name": "Reference Agent",
        "transport": "web",
        "max_concurrency": 1,
        # B2.7-12: verbatim reuse of the reference agent's own instructions
        # (app.engine.reference_agent.agent), same convention as
        # CARD_BLOCK_PERSONA elsewhere in this file -- generation needs
        # real prompt text to generate real drafts from, and this is the
        # one agent that actually has any.
        "prompt": (
            GREETER_INSTRUCTIONS
            + "\n\n"
            + BANKER_INSTRUCTIONS_TEMPLATE.format(first_name="the caller")
        ),
        "description": (
            "Cadence Bank's phone support agent. Verifies caller identity "
            "(full name, date of birth, security phrase) before discussing "
            "any account. Once verified, can check account balance, block a "
            "lost or stolen card, share branch hours/location, look up "
            "another customer's masked summary on request, or hand off to a "
            "human specialist."
        ),
    },
]

_PERSONAS: list[dict[str, Any]] = [
    {
        "key": "persona:priya",
        "name": "Priya Sharma",
        # B2.7-02/migration 011: real Chirp3-HD voice + BCP-47 locale
        # (was the OpenAI voice name "alloy" + bare "en" -- never
        # validated against anything until the engine actually read
        # these columns).
        "voice": "en-IN-Chirp3-HD-Achernar",
        "language": "en-IN",
        "accent": "Indian English",
        "traits": {"tone": "polite", "patience": "medium"},
    },
    {
        "key": "persona:frank",
        "name": "Frustrated Frank",
        "voice": "en-US-Chirp3-HD-Charon",
        "language": "en-US",
        "accent": "American English",
        "traits": {"tone": "impatient", "patience": "low"},
    },
    {
        "key": "persona:elena",
        "name": "Elderly Elena",
        "voice": "en-GB-Chirp3-HD-Achernar",
        "language": "en-GB",
        "accent": "British English",
        "traits": {"tone": "confused", "patience": "high"},
    },
    {
        # B2.7-11: same row migration 015's data step inserts directly
        # (matching deterministic id) -- kept here too so a fresh
        # environment seeded from scratch (no migration history) still
        # gets it, and so ON CONFLICT DO NOTHING makes the two coincide
        # rather than duplicate on an environment that ran both.
        "key": "persona:asha",
        "name": CARD_BLOCK_PERSONA.name,
        "voice": "en-US-Chirp3-HD-Charon",
        "language": "en-US",
        "accent": None,
        "traits": CARD_BLOCK_PERSONA.traits,
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
    {
        "key": "suite:reference",
        "agent_key": "agent:reference",
        "name": "Reference Agent Validation",
        "description": "Real scenarios against the actual reference agent worker.",
    },
]

_SCENARIOS: list[dict[str, Any]] = [
    {
        "key": "scenario:support:card-block",
        "suite_key": "suite:support",
        "name": "Lost card, request block",
        "persona_key": "persona:priya",
    },
    {
        "key": "scenario:support:balance",
        "suite_key": "suite:support",
        "name": "Balance inquiry",
        "persona_key": "persona:frank",
    },
    {
        "key": "scenario:support:account-update",
        "suite_key": "suite:support",
        "name": "Update contact details",
        "persona_key": "persona:elena",
    },
    {
        "key": "scenario:billing:dispute",
        "suite_key": "suite:billing",
        "name": "Dispute a charge",
        "persona_key": "persona:priya",
    },
    {
        "key": "scenario:billing:payment-plan",
        "suite_key": "suite:billing",
        "name": "Set up a payment plan",
        "persona_key": "persona:frank",
    },
    {
        "key": "scenario:billing:refund",
        "suite_key": "suite:billing",
        "name": "Request a refund",
        "persona_key": "persona:elena",
    },
    {
        "key": "scenario:reference:card-block",
        "suite_key": "suite:reference",
        "name": "Lost card, request block (reference agent)",
        "persona_key": "persona:asha",
        # B2-08: script is the executor's PersonaSpec input (everything
        # PersonaSpec needs besides `name`, which comes from `persona`
        # above) -- goal/openingLine/traits, reusing CARD_BLOCK_PERSONA
        # verbatim rather than re-authoring a second copy of this text.
        "script": {
            "goal": CARD_BLOCK_PERSONA.goal,
            "openingLine": CARD_BLOCK_PERSONA.opening_line,
            "traits": CARD_BLOCK_PERSONA.traits,
        },
        "assertions": [
            {
                "id": spec.id,
                "name": spec.name,
                "description": spec.description,
                "distinguishFrom": spec.distinguish_from,
            }
            for spec in (A1_REQUESTS_VERIFICATION, A2_CONFIRMS_NEXT_STEPS)
        ],
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
                "INSERT INTO agents "
                "(id, name, transport, max_concurrency, prompt, description, "
                " created_by_user_id) "
                "VALUES (:id, :name, :transport, :max_concurrency, :prompt, :description, "
                " :user_id) "
                # B2.7-12: prompt/description are new columns on a row this
                # seed script already created in every environment run
                # before this ticket -- ON CONFLICT DO NOTHING alone would
                # silently never backfill them onto an existing row.
                # Updating just these two (not name/transport/etc, which a
                # real user could have since edited) keeps a re-run
                # idempotent with this file's own constants.
                "ON CONFLICT (id) DO UPDATE SET "
                "prompt = EXCLUDED.prompt, description = EXCLUDED.description"
            ),
            {
                "id": agent_id,
                "name": a["name"],
                "transport": a["transport"],
                "max_concurrency": a["max_concurrency"],
                "prompt": a.get("prompt"),
                "description": a.get("description"),
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


async def _seed_metrics(conn: AsyncConnection) -> None:
    """B2.7-05: the nine built-in metrics (app.engine.metrics.builtins),
    same dual-path convention as _seed_personas -- content rows a fresh
    environment gets from this script, not from a schema migration."""
    for m in BUILTIN_METRICS:
        await conn.execute(
            text(
                "INSERT INTO metrics "
                "(id, project_id, agent_id, name, description, kind, output_type, "
                " spec, sampling_pct, status, version, created_by_user_id, builtin) "
                "VALUES (:id, NULL, NULL, :name, :description, :kind, :output_type, "
                " CAST(:spec AS jsonb), 100, 'active', 1, :user_id, true) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {
                "id": _id(f"metric:{m.key}"),
                "name": m.name,
                "description": m.docs,
                "kind": m.kind,
                "output_type": m.output_type,
                "spec": json.dumps(m.spec),
                "user_id": _SEED_USER_ID,
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
        # Most scenarios here are dashboard filler with bare {id, name}
        # assertions and no script -- only scenario:reference:card-block
        # (see _SCENARIOS) overrides both with real, judge-ready content.
        script = sc.get("script")
        await conn.execute(
            text(
                "INSERT INTO scenarios "
                "(id, suite_id, name, persona_id, script, assertions, source) "
                "VALUES (:id, :suite_id, :name, :persona_id, "
                " CAST(:script AS jsonb), CAST(:assertions AS jsonb), 'manual') "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {
                "id": scenario_id,
                "suite_id": suite_ids[sc["suite_key"]],
                "name": sc["name"],
                "persona_id": _id(sc["persona_key"]),
                "script": json.dumps(script) if script is not None else None,
                "assertions": json.dumps(sc.get("assertions", _SEED_ASSERTIONS)),
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
        await _seed_metrics(conn)
        suite_ids = await _seed_suites(conn, agent_ids)
        scenario_ids = await _seed_scenarios(conn, suite_ids)

    for run in _SEEDED_RUNS:
        await _seed_run(engine, run, agent_ids, scenario_ids)


# --- B2.8-05: `--demo` dashboard-population data (~200 calls / ~20 batches
# over 30 days), additive on top of the base `seed()` above, so W2.8-02's
# low-n empty states still get to see a fresh project with none of this. ---

_DEMO_AGENT: dict[str, Any] = {
    "key": "agent:collections",
    "name": "Collections Bot",
    "transport": "web",
    "max_concurrency": 2,
}

# No suite ever targets this agent -- deliberately: it never gets a real
# or synthetic completed call, so app.api.agents.derive_agent_liveness
# reports it "Offline"/"Never probed", giving the Agent Health card the
# same real mixed-health texture the redesign mock shows (not just four
# green dots), for free rather than by faking a status column.

# suite:reference is excluded -- B2-08's own docstring: it's "the one
# scenario a real executor can actually run", not synthetic filler.
_DEMO_BATCH_SUITE_KEYS = ["suite:support", "suite:billing"]
_DEMO_BATCH_COUNT = 20
_DEMO_BATCH_SIZE_RANGE = (8, 12)  # ~20 * 10 avg =~ 200 calls
_DEMO_WINDOW_DAYS = 30
_DEMO_SEED = 20260930  # fixed PRNG seed -- reruns draw the identical sequence

_DEMO_FINDING_CATEGORIES = ["pii_exfiltration", "prompt_injection"]


def _demo_scenario_ids_by_suite() -> dict[str, list[uuid.UUID]]:
    by_suite: dict[str, list[uuid.UUID]] = {}
    for sc in _SCENARIOS:
        by_suite.setdefault(sc["suite_key"], []).append(_id(sc["key"]))
    return by_suite


def _demo_batch_specs(rng: random.Random) -> list[tuple[str, int, int]]:
    """(suite_key, days_ago, batch_size) for each of the ~20 synthetic
    batches, spread roughly evenly across the trailing 30 days (with jitter,
    not a rigid grid) so trend lines show real day-to-day variance rather
    than a flat line -- the ticket's own "done when" bar."""
    specs = []
    for i in range(_DEMO_BATCH_COUNT):
        suite_key = _DEMO_BATCH_SUITE_KEYS[i % len(_DEMO_BATCH_SUITE_KEYS)]
        days_ago = min(
            _DEMO_WINDOW_DAYS - 1,
            int(i * _DEMO_WINDOW_DAYS / _DEMO_BATCH_COUNT) + rng.randint(0, 1),
        )
        batch_size = rng.randint(*_DEMO_BATCH_SIZE_RANGE)
        specs.append((suite_key, days_ago, batch_size))
    return specs


def _demo_child_outcome(rng: random.Random) -> tuple[str, str | None]:
    """Realistic pass/warn/fail/cancelled spread for one call: mostly pass,
    a meaningful review/fail tail, a rare cancellation -- not a coin flip,
    so pass-rate widgets show a believable ~80% rather than ~50%."""
    r = rng.random()
    if r < 0.78:
        return "completed", "pass"
    if r < 0.90:
        return "completed", "warn"
    if r < 0.97:
        return "completed", "fail"
    return "cancelled", None


async def _seed_demo_agent(conn: AsyncConnection) -> uuid.UUID:
    agent_id = _id(_DEMO_AGENT["key"])
    await conn.execute(
        text(
            "INSERT INTO agents (id, name, transport, max_concurrency, created_by_user_id) "
            "VALUES (:id, :name, :transport, :max_concurrency, :user_id) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {
            "id": agent_id,
            "name": _DEMO_AGENT["name"],
            "transport": _DEMO_AGENT["transport"],
            "max_concurrency": _DEMO_AGENT["max_concurrency"],
            "user_id": _SEED_USER_ID,
        },
    )
    return agent_id


async def _seed_demo_batch(
    conn: AsyncConnection,
    *,
    index: int,
    suite_key: str,
    agent_id: uuid.UUID,
    scenario_ids: list[uuid.UUID],
    days_ago: int,
    batch_size: int,
    apply_rubric: bool,
    rng: random.Random,
    now: datetime,
) -> list[uuid.UUID]:
    """Inserts one synthetic parent + `batch_size` children directly via SQL
    (not FakeRunner -- these rows are born "already completed", there's no
    in-flight state for anything downstream to race with), fully
    ON CONFLICT DO NOTHING so a second `--demo` run is a no-op. Returns the
    child ids that landed 'completed', for `_seed_demo_findings` to pick
    from."""
    parent_id = _id(f"demo:batch:{index}")
    batch_created_at = now - timedelta(days=days_ago, hours=rng.randint(0, 20))

    # Insert the parent (metrics filled in below, once children's verdicts
    # are known) before any child -- `runs.parent_run_id` is a real FK, and
    # every child row below points at this id.
    await conn.execute(
        text(
            "INSERT INTO runs "
            "(id, type, status, agent_id, created_at, created_by_user_id) "
            "VALUES (:id, 'suite', 'completed', :agent_id, :created_at, :user_id) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {
            "id": parent_id,
            "agent_id": agent_id,
            "created_at": batch_created_at,
            "user_id": _SEED_USER_ID,
        },
    )

    child_verdicts: list[str] = []
    completed_child_ids: list[uuid.UUID] = []
    for j in range(batch_size):
        call_id = _id(f"demo:call:{index}:{j}")
        scenario_id = scenario_ids[j % len(scenario_ids)]
        status, badge = _demo_child_outcome(rng)
        created_at = batch_created_at + timedelta(minutes=j)
        cost = round(rng.uniform(0.04, 0.22), 4) if status == "completed" else None
        metrics = {"resultBadge": badge} if badge else None

        await conn.execute(
            text(
                "INSERT INTO runs "
                "(id, type, status, agent_id, scenario_id, parent_run_id, created_at, "
                " metrics, cost, created_by_user_id) "
                "VALUES (:id, 'suite', :status, :agent_id, :scenario_id, :parent_id, "
                " :created_at, CAST(:metrics AS jsonb), CAST(:cost AS jsonb), :user_id) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {
                "id": call_id,
                "status": status,
                "agent_id": agent_id,
                "scenario_id": scenario_id,
                "parent_id": parent_id,
                "created_at": created_at,
                "metrics": json.dumps(metrics) if metrics else None,
                "cost": json.dumps({"estUsd": cost}) if cost is not None else None,
                "user_id": _SEED_USER_ID,
            },
        )
        if status == "completed":
            completed_child_ids.append(call_id)
            if badge:
                child_verdicts.append(badge)

            # A handful of agent-side turns with a realistic latency spread
            # (occasionally over the 1,000ms threshold, so the latency
            # histogram/flagged-turns card has real texture, not a flat
            # distribution).
            for idx in range(rng.randint(2, 4)):
                latency_ms = (
                    rng.randint(1100, 1900) if rng.random() < 0.12 else rng.randint(350, 950)
                )
                await conn.execute(
                    text(
                        "INSERT INTO turns (run_id, idx, role, text, latency_ms) "
                        "VALUES (:run_id, :idx, 'agent', 'Demo agent turn', :latency_ms) "
                        "ON CONFLICT DO NOTHING"
                    ),
                    {"run_id": call_id, "idx": idx * 2, "latency_ms": latency_ms},
                )

            # Independent-of-badge assertion failures -- gives "Top failing
            # checks" a believable, non-zero fail rate per check.
            for assertion in _SEED_ASSERTIONS:
                fail_chance = 0.15 if assertion["id"] == "a1" else 0.08
                a_status = "failed" if rng.random() < fail_chance else "passed"
                await conn.execute(
                    text(
                        "INSERT INTO assertion_results (run_id, assertion_id, name, status) "
                        "VALUES (:run_id, :assertion_id, :name, :status) "
                        "ON CONFLICT DO NOTHING"
                    ),
                    {
                        "run_id": call_id,
                        "assertion_id": assertion["id"],
                        "name": assertion["name"],
                        "status": a_status,
                    },
                )

    # rollup.py's real default (no rubric configured) always rolls a batch
    # up to 'pass' regardless of individual child failures -- replicated
    # here, not invented, so the demo's Recent Test Runs matches production
    # behaviour. `suite:billing` gets a rubric (see seed_demo) so at least
    # one suite's batches show a real, rubric-driven 'fail' too.
    if apply_rubric and child_verdicts:
        pass_rate = sum(1 for v in child_verdicts if v == "pass") / len(child_verdicts)
        parent_badge = "fail" if pass_rate < 0.85 else "pass"
    else:
        parent_badge = "pass"

    await conn.execute(
        text("UPDATE runs SET metrics = CAST(:metrics AS jsonb) WHERE id = :id"),
        {"id": parent_id, "metrics": json.dumps({"resultBadge": parent_badge})},
    )
    return completed_child_ids


async def _seed_demo_findings(conn: AsyncConnection, run_ids: list[uuid.UUID]) -> None:
    """Two open critical findings (ticket's own "done when"), each on a
    real completed demo call -- `findings.run_id` has no other reachable
    project scope, so a dashboard query that joins through `runs` picks
    these up naturally."""
    for i, (run_id, category) in enumerate(zip(run_ids, _DEMO_FINDING_CATEGORIES, strict=True)):
        await conn.execute(
            text(
                "INSERT INTO findings "
                "(id, run_id, category, severity, verdict, attack_prompt, agent_response, "
                " evidence, suggested_fix) "
                "VALUES (:id, :run_id, :category, 'critical', 'leaked', :prompt, :response, "
                " :evidence, :fix) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {
                "id": _id(f"demo:finding:{i}"),
                "run_id": run_id,
                "category": category,
                "prompt": "Demo adversarial prompt used to seed a realistic dashboard.",
                # Encrypted like every real finding (B4-05) so the demo path
                # exercises the same read code; readers also tolerate the
                # plaintext rows an earlier seed already wrote.
                "response": encrypt_text(
                    "Demo agent response that (in this seeded fixture) leaked data."
                ),
                "evidence": encrypt_text("Demo verbatim evidence quote."),
                "fix": "Demo suggested fix: tighten the relevant system-prompt guardrail.",
            },
        )


async def seed_demo(engine: AsyncEngine) -> None:
    """B2.8-05. Requires `seed()` to have already run (reuses its agents/
    suites/scenarios/personas ids) -- `main()` below always calls both, in
    order, so `--demo` alone is never a valid state to leave the DB in."""
    rng = random.Random(_DEMO_SEED)
    now = datetime.now(UTC)
    scenario_ids_by_suite = _demo_scenario_ids_by_suite()
    batch_specs = _demo_batch_specs(rng)

    async with engine.connect() as conn, conn.begin():
        await _seed_demo_agent(conn)
        billing_suite_id = _id("suite:billing")
        await conn.execute(
            text("UPDATE suites SET rubric = CAST(:rubric AS jsonb) WHERE id = :id"),
            {"id": billing_suite_id, "rubric": json.dumps({"minPassRate": 0.85})},
        )

        all_completed_ids: list[uuid.UUID] = []
        for i, (suite_key, days_ago, batch_size) in enumerate(batch_specs):
            agent_id = _id(_SUITE_TO_AGENT[suite_key])
            completed_ids = await _seed_demo_batch(
                conn,
                index=i,
                suite_key=suite_key,
                agent_id=agent_id,
                scenario_ids=scenario_ids_by_suite[suite_key],
                days_ago=days_ago,
                batch_size=batch_size,
                apply_rubric=(suite_key == "suite:billing"),
                rng=rng,
                now=now,
            )
            all_completed_ids.extend(completed_ids)

        await _seed_demo_findings(conn, all_completed_ids[:2])


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--demo",
        action="store_true",
        help="also seed ~200 synthetic calls across ~20 batches over 30 days (B2.8-05), "
        "so a fresh dashboard isn't empty. Never point this at a real production "
        "DATABASE_URL -- it's fixture data, not a real customer's history.",
    )
    args = parser.parse_args()

    engine = get_engine()
    await seed(engine)
    print(
        "Seed complete: 3 agents, 3 suites, 7 scenarios, 3 personas, 9 metrics, 4 completed runs."
    )
    if args.demo:
        await seed_demo(engine)
        print(
            "Demo data complete: +1 agent, ~20 batches / ~200 calls over 30 days, "
            "2 open critical findings."
        )


if __name__ == "__main__":
    asyncio.run(main())
