"""005_project_scoping_hard

Revision ID: 9f1c2a7b3e41
Revises: a350430ed7e9
Create Date: 2026-08-11 00:00:00.000000

B2.5-01: project_id becomes NOT NULL on suites/agents/runs -- B2-06 already
backfilled every row to DEFAULT_PROJECT_ID, so this is constraint-only, not
a data migration. Also adds projects.deleted_at (soft delete, additive) and
covering indexes on project_id since every list endpoint now filters on it.

Also swaps `runs.idempotency_key`'s UNIQUE constraint from single-column to
`(project_id, idempotency_key)`: with hard scoping, an Idempotency-Key
collision across two different projects must create two runs, not silently
hand project B the parent id of project A's request (the global constraint
would make `_create_run`'s ON CONFLICT DO NOTHING fire across projects).

Deliberately NOT touched here (see Cadence_stitch_protocol.md §5 -- every
B2.5-B2.8 migration is additive or constraint-only): run_events, turns,
assertion_results, findings scope transitively through run_id, not
project_id directly.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9f1c2a7b3e41"
down_revision: str | Sequence[str] | None = "a350430ed7e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("projects", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))

    for table in ("agents", "suites", "runs"):
        op.alter_column(table, "project_id", existing_type=sa.Uuid(), nullable=False)
        op.create_index(op.f(f"ix_{table}_project_id"), table, ["project_id"])

    op.drop_constraint(op.f("uq_runs_idempotency_key"), "runs", type_="unique")
    op.create_unique_constraint(
        "uq_runs_project_id_idempotency_key", "runs", ["project_id", "idempotency_key"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("uq_runs_project_id_idempotency_key", "runs", type_="unique")
    op.create_unique_constraint(op.f("uq_runs_idempotency_key"), "runs", ["idempotency_key"])

    for table in ("agents", "suites", "runs"):
        op.drop_index(op.f(f"ix_{table}_project_id"), table_name=table)
        op.alter_column(table, "project_id", existing_type=sa.Uuid(), nullable=True)

    op.drop_column("projects", "deleted_at")
