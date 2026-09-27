"""010_authoring_schema

Revision ID: 79c979f32a2d
Revises: bd2245aea43c
Create Date: 2026-09-27 15:52:09.247343

B2.7-01: the authoring schema. All additive -- new tables (metrics,
test_profiles, scenario_metrics) plus nullable columns on personas/
scenarios/suites. Nothing here is enforced yet (no code reads `goal`,
`rubric`, or scenario_metrics); that's the rest of B2.7.

`scenarios.persona`/`persona_initials` are kept and NOT backfilled away --
B2.7-11 drops them once every write path dual-writes `persona_id`. This
migration does backfill `persona_id` itself for existing rows, by matching
`scenarios.persona` (name) against `personas.name`: cheap, safe (read-only
until B2.7-02+ ships), and it means B2.7-02's CRUD work isn't the first
thing to populate it. One scenario (the reference-agent one, persona "Asha
Rao") has no matching personas row -- it predates that table entirely --
and is left with `persona_id IS NULL` rather than inventing a row here;
whoever builds B2.7-02 should decide whether to seed one.

Autogenerate also proposed dropping five indexes (ix_agents_project_id,
ix_projects_org_id, ix_runs_parent_run_id, ix_runs_project_id,
ix_suites_project_id) that exist in the DB (migrations 005/006) but were
never declared with `index=True` on their model columns -- a pre-existing
model/DB drift, unrelated to this ticket. Left alone here; the migration
below only contains this ticket's actual changes. (autogenerate also
proposed dropping `runs.recording_url` for the same reason -- that one *is*
fixed, in app/models/runs.py, since it's this session's own column.)
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "79c979f32a2d"
down_revision: str | Sequence[str] | None = "bd2245aea43c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "test_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "fields", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("encrypted", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_by_user_id", sa.Text(), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_test_profiles_project_id_projects")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_test_profiles")),
    )
    op.create_table(
        "metrics",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("output_type", sa.Text(), nullable=False),
        sa.Column(
            "spec", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("sampling_pct", sa.Integer(), server_default="100", nullable=False),
        sa.Column("status", sa.Text(), server_default="draft", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by_user_id", sa.Text(), nullable=False),
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
        sa.CheckConstraint(
            "kind IN ('builtin', 'llm_judge', 'python')", name=op.f("ck_metrics_kind_valid")
        ),
        sa.CheckConstraint(
            "output_type IN ('boolean', 'numeric', 'enum', 'tri_state')",
            name=op.f("ck_metrics_output_type_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'archived')", name=op.f("ck_metrics_status_valid")
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], name=op.f("fk_metrics_agent_id_agents"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_metrics_project_id_projects")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_metrics")),
    )
    # Two partial unique indexes, not one plain UNIQUE(project_id, agent_id,
    # name) -- see app/models/metrics.py's Metric.__table_args__ comment for
    # why a plain composite constraint can't enforce "at most one
    # project-level metric per name" once agent_id is nullable.
    op.create_index(
        "uq_metrics_project_agent_name",
        "metrics",
        ["project_id", "agent_id", "name"],
        unique=True,
        postgresql_where="agent_id IS NOT NULL",
    )
    op.create_index(
        "uq_metrics_project_name_no_agent",
        "metrics",
        ["project_id", "name"],
        unique=True,
        postgresql_where="agent_id IS NULL",
    )
    op.create_table(
        "scenario_metrics",
        sa.Column("scenario_id", sa.Uuid(), nullable=False),
        sa.Column("metric_id", sa.Uuid(), nullable=False),
        sa.Column("gating", sa.Boolean(), server_default="false", nullable=False),
        sa.ForeignKeyConstraint(
            ["metric_id"],
            ["metrics.id"],
            name=op.f("fk_scenario_metrics_metric_id_metrics"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["scenario_id"],
            ["scenarios.id"],
            name=op.f("fk_scenario_metrics_scenario_id_scenarios"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("scenario_id", "metric_id", name=op.f("pk_scenario_metrics")),
    )

    op.add_column("personas", sa.Column("project_id", sa.Uuid(), nullable=True))
    op.add_column("personas", sa.Column("emotion", sa.Text(), nullable=True))
    op.add_column("personas", sa.Column("speaking_rate", sa.Float(), nullable=True))
    op.add_column("personas", sa.Column("interruption_behavior", sa.Text(), nullable=True))
    op.add_column("personas", sa.Column("environment", sa.Text(), nullable=True))
    op.add_column(
        "personas", sa.Column("code_switch", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.create_foreign_key(
        op.f("fk_personas_project_id_projects"), "personas", "projects", ["project_id"], ["id"]
    )

    op.add_column("scenarios", sa.Column("goal", sa.Text(), nullable=True))
    op.add_column("scenarios", sa.Column("persona_id", sa.Uuid(), nullable=True))
    op.add_column("scenarios", sa.Column("test_profile_id", sa.Uuid(), nullable=True))
    op.add_column(
        "scenarios", sa.Column("conditions", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.create_foreign_key(
        op.f("fk_scenarios_test_profile_id_test_profiles"),
        "scenarios",
        "test_profiles",
        ["test_profile_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        op.f("fk_scenarios_persona_id_personas"), "scenarios", "personas", ["persona_id"], ["id"]
    )
    # Backfill by name match -- see this migration's module docstring for
    # why one row (the reference-agent scenario) is deliberately left NULL.
    op.execute(
        "UPDATE scenarios SET persona_id = personas.id "
        "FROM personas WHERE scenarios.persona = personas.name"
    )

    op.add_column(
        "suites", sa.Column("rubric", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("suites", "rubric")

    op.drop_constraint(op.f("fk_scenarios_persona_id_personas"), "scenarios", type_="foreignkey")
    op.drop_constraint(
        op.f("fk_scenarios_test_profile_id_test_profiles"), "scenarios", type_="foreignkey"
    )
    op.drop_column("scenarios", "conditions")
    op.drop_column("scenarios", "test_profile_id")
    op.drop_column("scenarios", "persona_id")
    op.drop_column("scenarios", "goal")

    op.drop_constraint(op.f("fk_personas_project_id_projects"), "personas", type_="foreignkey")
    op.drop_column("personas", "code_switch")
    op.drop_column("personas", "environment")
    op.drop_column("personas", "interruption_behavior")
    op.drop_column("personas", "speaking_rate")
    op.drop_column("personas", "emotion")
    op.drop_column("personas", "project_id")

    op.drop_table("scenario_metrics")
    op.drop_index(
        "uq_metrics_project_name_no_agent",
        table_name="metrics",
        postgresql_where="agent_id IS NULL",
    )
    op.drop_index(
        "uq_metrics_project_agent_name",
        table_name="metrics",
        postgresql_where="agent_id IS NOT NULL",
    )
    op.drop_table("metrics")
    op.drop_table("test_profiles")
