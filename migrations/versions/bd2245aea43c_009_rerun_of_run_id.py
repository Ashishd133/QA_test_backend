"""009_rerun_of_run_id

Revision ID: bd2245aea43c
Revises: 35efca88855b
Create Date: 2026-08-12 00:00:00.000000

B2.6-04: adds `runs.rerun_of_run_id`, a nullable self-FK recording which
run a rerun was cloned from (a single-run rerun points at the run it
reran; a batch rerun's new parent points at the original batch parent).
Additive, nullable, no backfill needed -- every existing row has no
rerun lineage. `ON DELETE SET NULL` matches `scenario_id`'s existing FK
(app/models/runs.py) -- losing the lineage pointer when the original run
is deleted is acceptable; blocking the delete is not.

This is E8's join point (a future diff view comparing a rerun's results
against what it reran), not consumed by anything yet.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "bd2245aea43c"
down_revision: str | Sequence[str] | None = "35efca88855b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("runs", sa.Column("rerun_of_run_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_runs_rerun_of_run_id_runs"),
        "runs",
        "runs",
        ["rerun_of_run_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f("fk_runs_rerun_of_run_id_runs"), "runs", type_="foreignkey")
    op.drop_column("runs", "rerun_of_run_id")
