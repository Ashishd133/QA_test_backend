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
    # B2.6-05: batch parent this run is a child of, or None for a
    # standalone Test Run (B2.5-03) -- the real executor
    # (app.engine.executor.simulation) sets it as a span attribute on every
    # span it creates so Phoenix can filter a whole batch's calls by one
    # id. Defaults to None so app.seed's direct ClaimedRun(...) construction
    # (always a standalone seeded run) doesn't need updating.
    parent_run_id: uuid.UUID | None = None


# B3-03: bounds how many *agents'* oldest-queued rows one claim attempt will
# consider. The candidate query is already one row per agent (see below), so
# this bounds distinct agents scanned, not queue depth -- a single agent
# with a 200+ call B2.6-01 batch no longer fills the whole window and
# starves every other agent's queued work.
_CANDIDATE_LIMIT = 200

# One row per agent -- just enough to know WHICH agents have queued,
# childless work and in what oldest-first order, not which specific run
# each will get (that's decided fresh, under the lock, in
# _CLAIM_OLDEST_FOR_AGENT_SQL below -- see claim_run's docstring for why a
# pre-picked id here was a real bug, not just unnecessary). Childless
# (NOT EXISTS ... parent_run_id) excludes batch/suite parent rows -- those
# get closed by app.workers.rollup, not claimed and executed themselves.
_CANDIDATE_AGENTS_SQL = text(
    "SELECT agent_id FROM ("
    "  SELECT DISTINCT ON (agent_id) agent_id, created_at"
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

# Picks and claims this agent's actual oldest queued row in one statement,
# under the per-agent lock already held by the caller -- see claim_run's
# docstring for why re-selecting here (not reusing the candidate scan's id)
# is required, not just extra caution. FOR UPDATE SKIP LOCKED is still
# correct/needed even under the advisory lock: it's what makes this
# statement itself safe to run concurrently with the reaper's own row
# locking (app.workers.reaper), which the per-agent advisory lock says
# nothing about.
_CLAIM_OLDEST_FOR_AGENT_SQL = text(
    "UPDATE runs SET status = 'claimed', claimed_by = :worker_id, heartbeat_at = now() "
    "WHERE id = ("
    "  SELECT r.id FROM runs r"
    "  WHERE r.agent_id = :agent_id AND r.status = 'queued'"
    "    AND NOT EXISTS (SELECT 1 FROM runs c WHERE c.parent_run_id = r.id)"
    "  ORDER BY r.created_at"
    "  LIMIT 1"
    "  FOR UPDATE SKIP LOCKED"
    ") "
    "RETURNING id, type, agent_id, scenario_id, config, parent_run_id"
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

    The row actually claimed is picked *fresh, under that lock*
    (_CLAIM_OLDEST_FOR_AGENT_SQL), not the id the candidate scan happened to
    see beforehand: with the scan returning only one row per agent, reusing
    that id meant a claimer that lost the race for it gave up on the whole
    agent for this call -- even with capacity and four more queued rows
    sitting right behind it. Concurrent claimers all targeting one agent
    would then collapse to strictly one-at-a-time, no matter the cap
    (caught by tests/test_suite_run_fanout.py's max-concurrency integration
    test, not by test_claim_concurrency.py's single-agent tests, which
    never has more queued rows for that agent than fit capacity anyway).

    Fairness: the candidate query is one row per agent (that agent's
    presence in the queue, not a specific run), so one capped or busy
    agent's backlog can never fill the scan window and starve every other
    agent's queued work the way a naive "just look at the single oldest
    queued row" claim would.
    """
    async with engine.connect() as conn, conn.begin():
        candidates = (
            (await conn.execute(_CANDIDATE_AGENTS_SQL, {"limit": _CANDIDATE_LIMIT}))
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
                        _CLAIM_OLDEST_FOR_AGENT_SQL,
                        {"agent_id": agent_id, "worker_id": worker_id},
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                # This agent's queue emptied out between the candidate scan
                # and here -- move on rather than retry it.
                continue
            return ClaimedRun(
                id=row["id"],
                type=row["type"],
                agent_id=row["agent_id"],
                scenario_id=row["scenario_id"],
                config=row["config"],
                parent_run_id=row["parent_run_id"],
            )
        return None
