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
`status`/`ended_at` once every child is terminal, plus a parent-scoped
`progress` event on every child completion. Until B2.7-08's rubric exists,
"any child failed" does NOT make the parent `failed` -- the ticket is
explicit that a parent goes `completed` with a failure count until then;
`aggregate.statusCounts` is where that count already lives. Cancellation
is the one exception: a parent closes `cancelled`, not `completed`, if any
of its children ended up cancelled -- distinct from the failed case because
it reflects an explicit user action (B2.6-02's cascade, or an individual
child cancelled one at a time) rather than an organic per-scenario outcome.
"""

import uuid
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.events import emit, progress_event, status_event

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

_CLOSE_PARENT_SQL = text(
    "UPDATE runs SET status = :status, ended_at = now() WHERE id = :id"
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
