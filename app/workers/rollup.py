"""B2.5-04: parent rollup on child completion.

`maybe_close_parent` is called from every place a child run's status flips
to a terminal value (completed/cancelled/failed) -- app.api.runs.cancel_run,
app.workers.fake_runner, app.engine.executor.simulation, and
app.workers.reaper -- always within the SAME transaction as that status
flip, so a parent's rollup can never observe a child transition that later
gets rolled back.

Read-computed, not persisted (B2.5-03's `aggregate` stays that way even
after this lands): the only thing this module *writes* is the parent's own
`status`/`ended_at` once every child is terminal, plus a parent-scoped
`progress` event on every child completion. Until B2.7-08's rubric exists,
"any child failed" does NOT make the parent `failed` -- the ticket is
explicit that a parent goes `completed` with a failure count until then;
`aggregate.statusCounts` is where that count already lives.
"""

import uuid

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
    "count(*) FILTER (WHERE status NOT IN ('completed', 'cancelled', 'failed')) AS live "
    "FROM runs WHERE parent_run_id = :parent_id"
)

_CLOSE_PARENT_SQL = text("UPDATE runs SET status = 'completed', ended_at = now() WHERE id = :id")


async def maybe_close_parent(conn: AsyncConnection, child_run_id: uuid.UUID) -> None:
    """No-op for a run with no parent (including parents themselves, which
    have `parent_run_id IS NULL`) or once the parent is already terminal.
    Call this immediately after committing a child's own terminal status
    write, in the same transaction.
    """
    child_row = (await conn.execute(_CHILD_PARENT_SQL, {"id": child_run_id})).mappings().first()
    if child_row is None or child_row["parent_run_id"] is None:
        return
    parent_id = child_row["parent_run_id"]

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

    await conn.execute(_CLOSE_PARENT_SQL, {"id": parent_id})
    await emit(conn, parent_id, status_event(status="completed"))
