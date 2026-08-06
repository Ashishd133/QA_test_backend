"""003_projects_cost_end_reason

Revision ID: 8b846bdde9ea
Revises: 4d1f291325bc
Create Date: 2026-08-06 13:44:02.855804

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "8b846bdde9ea"
down_revision: str | Sequence[str] | None = "4d1f291325bc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Must match app.models.projects.DEFAULT_PROJECT_ID (uuid5 of a stable key,
# same pattern as app/seed.py) -- the migration and the application need to
# agree on this id without a runtime lookup.
_DEFAULT_PROJECT_ID = "9c6cf0c0-8045-5954-8b3b-f820af1fd9d3"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("org_id", sa.Text(), server_default="org_default", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_projects")),
    )
    op.execute(
        sa.text(
            "INSERT INTO projects (id, name) VALUES (CAST(:id AS uuid), 'Default Project') "
            "ON CONFLICT (id) DO NOTHING"
        ).bindparams(id=_DEFAULT_PROJECT_ID)
    )

    for table in ("agents", "suites", "runs"):
        op.add_column(
            table,
            sa.Column(
                "project_id",
                sa.Uuid(),
                server_default=_DEFAULT_PROJECT_ID,
                nullable=True,
            ),
        )
        # Explicit, not relied on implicitly: PG 11+'s ADD COLUMN ... DEFAULT
        # already backfills existing rows for reads, but the ticket calls
        # out "backfill verified" as its own acceptance point, so make it a
        # real, auditable UPDATE rather than resting on that engine detail.
        op.execute(
            sa.text(
                f"UPDATE {table} SET project_id = CAST(:id AS uuid) WHERE project_id IS NULL"
            ).bindparams(id=_DEFAULT_PROJECT_ID)
        )
        op.create_foreign_key(
            op.f(f"fk_{table}_project_id_projects"), table, "projects", ["project_id"], ["id"]
        )

    op.add_column("runs", sa.Column("end_reason", sa.Text(), nullable=True))
    op.add_column("runs", sa.Column("cost", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_check_constraint(
        op.f("ck_runs_end_reason_valid"),
        "runs",
        "end_reason IS NULL OR end_reason IN "
        "('completed', 'caller_ended', 'agent_ended', 'timeout', 'cancelled', 'error')",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f("ck_runs_end_reason_valid"), "runs", type_="check")
    op.drop_column("runs", "cost")
    op.drop_column("runs", "end_reason")
    for table in ("runs", "suites", "agents"):
        op.drop_constraint(op.f(f"fk_{table}_project_id_projects"), table, type_="foreignkey")
        op.drop_column(table, "project_id")
    op.drop_table("projects")
