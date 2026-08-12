"""008_run_triggers

Revision ID: 35efca88855b
Revises: c1a4f6e0b823
Create Date: 2026-08-12 00:00:00.000000

B2.6-03: adds `runs.trigger` ('manual'|'schedule'|'ci'|'api', default
'manual') so the History table's trigger chip -- and E3's GitHub Action /
`schedules` table, once those land -- can read how a run was started
without inspecting `config`. Additive, NOT NULL with a server_default, so
every existing row backfills to 'manual' (correct: nothing that created a
run before this ticket was anything else) with no separate data migration
step.

Also adds an index on `runs.parent_run_id`: B3-03's claim query and
B2.5-04/B2.6-02's rollup tally both filter/join on it already (the
"childless" claimability check, the child tally), and it was never
covered by an index before now -- folded in here per Cadence_stitch_
protocol.md §5 rather than as its own migration, since this is the next
runs-table migration to land after fan-out made that predicate load-
bearing.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "35efca88855b"
down_revision: str | Sequence[str] | None = "c1a4f6e0b823"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "runs",
        sa.Column("trigger", sa.Text(), nullable=False, server_default="manual"),
    )
    op.create_check_constraint(
        "trigger_valid", "runs", "trigger IN ('manual', 'schedule', 'ci', 'api')"
    )
    op.create_index(op.f("ix_runs_parent_run_id"), "runs", ["parent_run_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_runs_parent_run_id"), table_name="runs")
    op.drop_constraint("trigger_valid", "runs", type_="check")
    op.drop_column("runs", "trigger")
