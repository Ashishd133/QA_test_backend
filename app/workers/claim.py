import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass(frozen=True)
class ClaimedRun:
    id: uuid.UUID
    type: str
    agent_id: uuid.UUID
    scenario_id: uuid.UUID | None
    config: dict[str, object]


# B3-03: bounds how many *agents'* oldest-queued rows one claim attempt will
# consider. The candidate query is already one row per agent (see below), so
# this bounds distinct agents scanned, not queue depth -- a single agent
# with a 200+ call B2.6-01 batch no longer fills the whole window and
# starves every other agent's queued work.
_CANDIDATE_LIMIT = 200

# One row per agent (its single oldest queued, childless run), oldest-agent
# first. Childless (NOT EXISTS ... parent_run_id) excludes batch/suite
# parent rows -- those get closed by app.workers.rollup, not claimed and
# executed themselves.
_CANDIDATE_QUEUED_RUNS_SQL = text(
    "SELECT id, agent_id FROM ("
    "  SELECT DISTINCT ON (agent_id) id, agent_id, created_at"
    "  FROM runs r"
    "  WHERE r.status = 'queued'"
    "    AND NOT EXISTS (SELECT 1 FROM runs c WHERE c.parent_run_id = r.id)"
    "  ORDER BY agent_id, created_at"
    ") oldest_per_agent"
    " ORDER BY created_at"
    " LIMIT :limit"
)

# Serializes claim attempts per agent -- see claim_run's docstring for why
# this, not a single global lock or a bare COUNT+UPDATE, is what makes
# max_concurrency actually hold under concurrent claimers. try_lock (not a
# blocking lock): a claimer that's already holding locks for earlier agents
# in this scan must never block waiting on a later agent's lock -- two
# workers locking agents in opposite orders under a blocking lock is a
# textbook Postgres deadlock. Losing the try just means skip this agent for
# this attempt; the next claim_run call tries again.
_TRY_LOCK_AGENT_SQL = text("SELECT pg_try_advisory_xact_lock(hashtext(:key)) AS acquired")

_AGENT_CAPACITY_SQL = text(
    "SELECT a.max_concurrency, "
    "(SELECT count(*) FROM runs r WHERE r.agent_id = a.id "
    " AND r.status IN ('claimed', 'running')) AS live "
    "FROM agents a WHERE a.id = :agent_id"
)

_CLAIM_SPECIFIC_RUN_SQL = text(
    "UPDATE runs SET status = 'claimed', claimed_by = :worker_id, heartbeat_at = now() "
    "WHERE id = :run_id AND status = 'queued' "
    "RETURNING id, type, agent_id, scenario_id, config"
)


async def claim_run(engine: AsyncEngine, worker_id: str) -> ClaimedRun | None:
    """Atomically claims the oldest claimable queued run, or None if none is
    available right now.

    B3-03: the B1-04 precheck at run-creation time is advisory only (spine:
    "fail-fast, not the authoritative gate") -- this is the real one.
    Enforcing `agent.max_concurrency` correctly against concurrent claimers
    needs more than the old "SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1"
    (which only ever serialized two workers onto the *same row*, never
    checked capacity at all): two workers racing to claim two *different*
    queued runs for the *same* agent can both read "0 live" before either
    commits, and both succeed -- a plain COUNT-then-UPDATE has that race
    built in.

    The fix: take a per-agent Postgres advisory lock (same
    `pg_advisory_xact_lock` family as app.events.emit's per-run lock, but
    the non-blocking `pg_try_advisory_xact_lock` -- see the try-lock comment
    above) *before* checking that agent's live-run count, so only one
    claimer at a time is ever inside the check-then-claim window for a
    given agent. Different agents still claim fully in parallel -- nothing
    here is a global lock.

    Fairness: the candidate query is one row per agent (that agent's oldest
    queued, childless run), so one capped or busy agent's backlog can never
    fill the scan window and starve every other agent's queued work the way
    a naive "just look at the single oldest queued row" claim would.
    """
    async with engine.connect() as conn, conn.begin():
        candidates = (
            (await conn.execute(_CANDIDATE_QUEUED_RUNS_SQL, {"limit": _CANDIDATE_LIMIT}))
            .mappings()
            .all()
        )

        for candidate in candidates:
            agent_id = candidate["agent_id"]

            acquired = (
                await conn.execute(_TRY_LOCK_AGENT_SQL, {"key": f"claim:{agent_id}"})
            ).scalar_one()
            if not acquired:
                # Another concurrent claimer already holds this agent's
                # lock -- skip rather than wait, so this call can never
                # deadlock against it. Try again on the next claim_run call.
                continue

            capacity = (
                (await conn.execute(_AGENT_CAPACITY_SQL, {"agent_id": agent_id})).mappings().one()
            )
            if capacity["live"] >= capacity["max_concurrency"]:
                continue

            row = (
                (
                    await conn.execute(
                        _CLAIM_SPECIFIC_RUN_SQL,
                        {"run_id": candidate["id"], "worker_id": worker_id},
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                # Claimed/cancelled by something else between the candidate
                # scan and here -- move on rather than retry this agent.
                continue
            return ClaimedRun(
                id=row["id"],
                type=row["type"],
                agent_id=row["agent_id"],
                scenario_id=row["scenario_id"],
                config=row["config"],
            )
        return None
