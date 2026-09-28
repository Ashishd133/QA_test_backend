"""B2.5-04: parent rollup on child completion. B2.6-02 adds the cancelled
close path.

`maybe_close_parent` is called from every place a CHILD run's status flips
to a terminal value (completed/cancelled/failed) -- app.api.runs.cancel_run,
app.workers.fake_runner, app.engine.executor.simulation, and
app.workers.reaper -- always within the SAME transaction as that status
flip, so a parent's rollup can never observe a child transition that later
gets rolled back. `close_parent_now` is the same close logic entered from
the PARENT's own id instead: B2.6-02's batch cancel cascades every live
child to 'cancelled' itself, synchronously, in one transaction -- there's
no later child-side status flip to hang maybe_close_parent's lookup off
of, so it calls this directly once it's done.

Read-computed, not persisted (B2.5-03's `aggregate` stays that way even
after this lands): the only thing this module *writes* is the parent's own
`status`/`ended_at`/`metrics.resultBadge` once every child is terminal,
plus a parent-scoped `progress` event on every child completion.
`status` (lifecycle) is unaffected by B2.7-08's rubric -- "any child
failed" still does NOT make the parent's own `status` `failed`; a batch
with real failures is still `status='completed'`, same as always
(`aggregate.statusCounts` is where that count already lives). What the
rubric changes is the parent's *verdict* (`metrics.resultBadge`, read by
`app.verdict.verdict_for_run` exactly like a call's own badge) -- without
a rubric configured on the batch's suite, that stays the pre-B2.7-08
default ("pass", per `evaluate_batch`'s own docstring) rather than
regressing anything. Cancellation is the one exception to `status` itself:
a parent closes `cancelled`, not `completed`, if any of its children ended
up cancelled -- distinct from the failed case because it reflects an
explicit user action (B2.6-02's cascade, or an individual child cancelled
one at a time) rather than an organic per-scenario outcome; a cancelled
batch gets no rubric verdict at all (there's nothing to judge).
"""

import json
import uuid
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.engine.rubric import evaluate_batch
from app.events import emit, progress_event, status_event
from app.verdict import verdict_for_run

_TERMINAL_STATUSES = ("completed", "cancelled", "failed")

_CHILD_PARENT_SQL = text("SELECT parent_run_id FROM runs WHERE id = :id")

# Locks the parent row for the rest of this transaction -- B2.6 runs
# children at up to max_concurrency in parallel, so two siblings can finish
# in overlapping transactions. Without this lock each transaction's tally
# can miss the other's not-yet-committed update and neither ever closes the
# parent (a lost-update race, not a hypothetical one once fan-out lands).
_LOCK_PARENT_SQL = text("SELECT status FROM runs WHERE id = :id FOR UPDATE")

_TALLY_SQL = text(
    "SELECT count(*) AS total, "
    "count(*) FILTER (WHERE status NOT IN ('completed', 'cancelled', 'failed')) AS live, "
    "count(*) FILTER (WHERE status = 'cancelled') AS cancelled "
    "FROM runs WHERE parent_run_id = :parent_id"
)

_CLOSE_PARENT_SQL = text("UPDATE runs SET status = :status, ended_at = now() WHERE id = :id")
_SET_PARENT_METRICS_SQL = text("UPDATE runs SET metrics = CAST(:metrics AS jsonb) WHERE id = :id")
_CHILD_STATUS_METRICS_SQL = text(
    "SELECT status, metrics FROM runs WHERE parent_run_id = :parent_id"
)
# One child's scenario -> suite is enough: a fan-out batch's children all
# come from the same suite (B2.6-01), so any one of them names the rubric
# that governs the whole batch. LIMIT 1 rather than DISTINCT: cheaper, and
# a batch whose children somehow span scenario_id IS NULL rows (redteam/
# discovery types never populate parent_run_id fan-outs today) just finds
# nothing and evaluate_batch's own no-rubric fallback applies.
_BATCH_RUBRIC_SQL = text(
    "SELECT s.rubric FROM runs r "
    "JOIN scenarios sc ON sc.id = r.scenario_id "
    "JOIN suites s ON s.id = sc.suite_id "
    "WHERE r.parent_run_id = :parent_id AND r.scenario_id IS NOT NULL "
    "LIMIT 1"
)


async def _close_if_done(conn: AsyncConnection, parent_id: uuid.UUID) -> None:
    parent_row = (await conn.execute(_LOCK_PARENT_SQL, {"id": parent_id})).mappings().first()
    if parent_row is None or parent_row["status"] in _TERMINAL_STATUSES:
        return

    tally = (await conn.execute(_TALLY_SQL, {"parent_id": parent_id})).mappings().one()
    completed_count = tally["total"] - tally["live"]
    await emit(
        conn, parent_id, progress_event(completed_count=completed_count, total_count=tally["total"])
    )

    if tally["live"] > 0:
        return

    final_status: Literal["cancelled", "completed"] = (
        "cancelled" if tally["cancelled"] > 0 else "completed"
    )
    await conn.execute(_CLOSE_PARENT_SQL, {"id": parent_id, "status": final_status})
    await emit(conn, parent_id, status_event(status=final_status))

    if final_status == "completed":
        # B2.7-08: a cancelled batch gets no rubric verdict -- there's
        # nothing to judge (same reasoning as a single cancelled call
        # never getting a result_badge either).
        rubric_row = (
            (await conn.execute(_BATCH_RUBRIC_SQL, {"parent_id": parent_id})).mappings().first()
        )
        rubric = rubric_row["rubric"] if rubric_row is not None else None
        child_rows = (
            (await conn.execute(_CHILD_STATUS_METRICS_SQL, {"parent_id": parent_id}))
            .mappings()
            .all()
        )
        child_verdicts = [
            v
            for v in (verdict_for_run(row["status"], row["metrics"]) for row in child_rows)
            if v != "idle"  # unreachable here (every child is terminal), kept for the type
        ]
        batch_verdict = evaluate_batch(child_verdicts, rubric=rubric, default_verdict="pass")
        await conn.execute(
            _SET_PARENT_METRICS_SQL,
            {"id": parent_id, "metrics": json.dumps({"resultBadge": batch_verdict})},
        )


async def maybe_close_parent(conn: AsyncConnection, child_run_id: uuid.UUID) -> None:
    """No-op for a run with no parent (including parents themselves, which
    have `parent_run_id IS NULL`) or once the parent is already terminal.
    Call this immediately after committing a child's own terminal status
    write, in the same transaction.
    """
    child_row = (await conn.execute(_CHILD_PARENT_SQL, {"id": child_run_id})).mappings().first()
    if child_row is None or child_row["parent_run_id"] is None:
        return
    await _close_if_done(conn, child_row["parent_run_id"])


async def close_parent_now(conn: AsyncConnection, parent_id: uuid.UUID) -> None:
    """B2.6-02: call with a batch parent's OWN id once every one of its
    children has already been made terminal in this same transaction (the
    cascade-cancel case) -- there's no child-side transition afterward for
    maybe_close_parent to be triggered from. No-op if `parent_id` has no
    children at all (tally.total == 0 makes tally.live == 0 trivially, but
    callers are expected to only call this for a run that actually has
    children -- see app.api.runs.cancel_run's has_children check)."""
    await _close_if_done(conn, parent_id)
