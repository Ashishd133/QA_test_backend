"""006_organizations_membership

Revision ID: b7d4e2a91c05
Revises: 9f1c2a7b3e41
Create Date: 2026-08-11 00:00:00.000000

B2.5-02: organizations + org_memberships, real rows instead of
OrgScopedMixin's hardcoded 'org_default' string. Scoped narrowly per
Cadence_stitch_protocol.md §5 -- only `projects.org_id` changes type (Text
-> uuid FK); the other seven OrgScopedMixin tables keep their placeholder
Text column untouched and continue to scope transitively through
project_id, not directly through org_id.

Membership backfill: every distinct `created_by_user_id` seen on
agents/suites/runs becomes an 'admin' member of the default org. Roles are
stored, not enforced (that's E10) -- but membership *existence* is what
app.deps.require_project_id checks from this migration on, so a user id
that never created anything here correctly sees zero orgs until someone
invites them; this backfill is what keeps every existing dev/test user
from being locked out on this exact migration.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7d4e2a91c05"
down_revision: str | Sequence[str] | None = "9f1c2a7b3e41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Must match app.models.organizations.DEFAULT_ORG_ID / .../projects.DEFAULT_PROJECT_ID.
_DEFAULT_ORG_ID = "397a6b98-7890-5c0a-9e3a-ff872e679079"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "organizations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organizations")),
        sa.UniqueConstraint("slug", name=op.f("uq_organizations_slug")),
    )
    op.execute(
        sa.text(
            "INSERT INTO organizations (id, name, slug) "
            "VALUES (CAST(:id AS uuid), 'Default Organization', 'org_default') "
            "ON CONFLICT (id) DO NOTHING"
        ).bindparams(id=_DEFAULT_ORG_ID)
    )

    op.create_table(
        "org_memberships",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), server_default="member", nullable=False),
        sa.CheckConstraint(
            "role IN ('admin', 'member', 'viewer')", name=op.f("ck_org_memberships_role_valid")
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["organizations.id"], name=op.f("fk_org_memberships_org_id_organizations")
        ),
        sa.PrimaryKeyConstraint("org_id", "user_id", name=op.f("pk_org_memberships")),
    )
    op.execute(
        sa.text(
            "INSERT INTO org_memberships (org_id, user_id, role) "
            "SELECT CAST(:org_id AS uuid), u.created_by_user_id, 'admin' FROM ("
            "  SELECT created_by_user_id FROM agents "
            "  UNION SELECT created_by_user_id FROM suites "
            "  UNION SELECT created_by_user_id FROM runs"
            ") AS u "
            "ON CONFLICT (org_id, user_id) DO NOTHING"
        ).bindparams(org_id=_DEFAULT_ORG_ID)
    )

    # server_default matches app.models.projects.Project.org_id -- an insert
    # that doesn't name org_id explicitly (same pattern as project_id's
    # server_default in migration 003) still lands rows in the default org.
    op.add_column(
        "projects",
        sa.Column("org_id_new", sa.Uuid(), server_default=_DEFAULT_ORG_ID, nullable=True),
    )
    op.execute(
        sa.text("UPDATE projects SET org_id_new = CAST(:id AS uuid)").bindparams(id=_DEFAULT_ORG_ID)
    )
    op.alter_column("projects", "org_id_new", existing_type=sa.Uuid(), nullable=False)
    op.drop_column("projects", "org_id")
    op.alter_column("projects", "org_id_new", new_column_name="org_id", existing_type=sa.Uuid())
    op.create_foreign_key(
        op.f("fk_projects_org_id_organizations"), "projects", "organizations", ["org_id"], ["id"]
    )
    op.create_index(op.f("ix_projects_org_id"), "projects", ["org_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_projects_org_id"), table_name="projects")
    op.drop_constraint(op.f("fk_projects_org_id_organizations"), "projects", type_="foreignkey")
    op.alter_column("projects", "org_id", new_column_name="org_id_new")
    op.add_column(
        "projects", sa.Column("org_id", sa.Text(), server_default="org_default", nullable=False)
    )
    op.drop_column("projects", "org_id_new")

    op.drop_table("org_memberships")
    op.drop_table("organizations")
