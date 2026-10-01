"""018_dashboard_v2

Revision ID: 4dee343ffc00
Revises: cfe480e20fa0
Create Date: 2026-09-30 15:14:53.112812

B2.8-01/04, purely additive -- safe to run while old code is live.

  1. `findings.resolved_at` (nullable timestamp, NULL = open). B4-04/B4-05
     (findings write path) don't specify a resolution field yet -- the
     backend backlog's B4 section is silent on it, so this stays a minimal,
     forward-compatible guess rather than a name B4 is committed to: a
     nullable "when resolved" timestamp, not a bool, so a future resolve
     endpoint has a timestamp to show for free. B2.8-01's dashboard card
     needs "open critical findings" to mean something before any findings
     actually exist (B2.8-05 seeds two), so this can't wait for B4.
  2. `projects.budget_usd_monthly` (nullable numeric, NULL = no budget
     configured). B2.8-04's own "done when": a project with no budget
     returns `null` and the UI hides the credits bar rather than showing a
     fake 0%.
  3. A composite `(project_id, created_at)` index on `runs` -- migrations
     005/006 already gave it single-column `project_id` and `parent_run_id`
     indexes (per migration 010's docstring), but every dashboard/runs-list
     query both filters on `project_id` *and* orders/windows by
     `created_at`, which the single-column index can't cover as well.
     `parent_run_id`'s existing index already covers the "is this row a
     real leaf call, not a batch's parent orchestrator" predicate
     (`NOT EXISTS (SELECT 1 FROM runs c WHERE c.parent_run_id = runs.id)`)
     B2.8-01 needs to avoid double-counting a batch parent's own rolled-up
     verdict alongside its children's -- a bug that was latent in v1
     (unfiltered `FROM runs`) until B2.6 batches made it real.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4dee343ffc00"
down_revision: str | Sequence[str] | None = "cfe480e20fa0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("findings", sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("projects", sa.Column("budget_usd_monthly", sa.Numeric(12, 2), nullable=True))
    op.create_index("ix_runs_project_id_created_at", "runs", ["project_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_runs_project_id_created_at", table_name="runs")
    op.drop_column("projects", "budget_usd_monthly")
    op.drop_column("findings", "resolved_at")
