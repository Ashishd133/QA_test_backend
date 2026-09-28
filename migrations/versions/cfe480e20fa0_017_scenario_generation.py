"""017_scenario_generation

Revision ID: cfe480e20fa0
Revises: 888504255212
Create Date: 2026-09-29 02:14:15.330681

B2.7-12: two additions.

`agents.prompt`/`description` -- the two distinct generation sources
W2.7-07's source picker offers (agent prompt vs. agent description),
neither of which existed as a stored field before this.

`discovery_drafts` restructured for generation, which is the first thing
that ever writes to this table (checked: nothing did before this commit).
`run_id` becomes nullable -- a `source='agent_prompt'`/`'agent_description'`
draft has no discovery run behind it, and a synthetic run row per generate
call would inflate the dashboard's testRuns7d (its query has no type
filter) and would show up in GET /v1/runs as a Test Run. `draft_id` alone
becomes the primary key (dropping `run_id` from it) -- this also makes
`add_scenario`'s existing "assumes draft_id is globally addressable"
comment (app/api/suites.py) true rather than a documented gap. `suite_id`
replaces `run_id` as what scopes a draft to a project. New columns carry
what generation proposes: persona_id (resolved at generation time where
possible), goal, script, proposed_metrics (resolved metric ids only).

Both DBs confirmed empty (0 rows) in discovery_drafts before this
migration, so the PK/nullability changes need no backfill.

Autogenerate again proposed dropping the same 5 pre-existing spurious
indexes -- still unrelated drift, still left alone. Autogenerate also
doesn't detect primary-key-column or CHECK-constraint changes, so the PK
rebuild and `source_valid` CHECK below are hand-written, not generated.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "cfe480e20fa0"
down_revision: str | Sequence[str] | None = "888504255212"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("agents", sa.Column("prompt", sa.Text(), nullable=True))
    op.add_column("agents", sa.Column("description", sa.Text(), nullable=True))

    op.add_column("discovery_drafts", sa.Column("suite_id", sa.Uuid(), nullable=False))
    op.add_column("discovery_drafts", sa.Column("source", sa.Text(), nullable=False))
    op.add_column("discovery_drafts", sa.Column("persona_id", sa.Uuid(), nullable=True))
    op.add_column("discovery_drafts", sa.Column("goal", sa.Text(), nullable=True))
    op.add_column(
        "discovery_drafts",
        sa.Column("script", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "discovery_drafts",
        sa.Column(
            "proposed_metrics",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
    )
    op.add_column(
        "discovery_drafts",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.add_column(
        "discovery_drafts",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.drop_constraint(op.f("pk_discovery_drafts"), "discovery_drafts", type_="primary")
    op.alter_column("discovery_drafts", "run_id", existing_type=sa.UUID(), nullable=True)
    op.create_primary_key(op.f("pk_discovery_drafts"), "discovery_drafts", ["draft_id"])

    op.create_check_constraint(
        "ck_discovery_drafts_source_valid",
        "discovery_drafts",
        "source IN ('agent_prompt', 'agent_description', 'discovery_run')",
    )

    op.drop_constraint(
        op.f("fk_discovery_drafts_added_scenario_id_scenarios"),
        "discovery_drafts",
        type_="foreignkey",
    )
    op.create_foreign_key(
        op.f("fk_discovery_drafts_added_scenario_id_scenarios"),
        "discovery_drafts",
        "scenarios",
        ["added_scenario_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        op.f("fk_discovery_drafts_persona_id_personas"),
        "discovery_drafts",
        "personas",
        ["persona_id"],
        ["id"],
    )
    op.create_foreign_key(
        op.f("fk_discovery_drafts_suite_id_suites"),
        "discovery_drafts",
        "suites",
        ["suite_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        op.f("fk_discovery_drafts_suite_id_suites"), "discovery_drafts", type_="foreignkey"
    )
    op.drop_constraint(
        op.f("fk_discovery_drafts_persona_id_personas"), "discovery_drafts", type_="foreignkey"
    )
    op.drop_constraint(
        op.f("fk_discovery_drafts_added_scenario_id_scenarios"),
        "discovery_drafts",
        type_="foreignkey",
    )
    op.create_foreign_key(
        op.f("fk_discovery_drafts_added_scenario_id_scenarios"),
        "discovery_drafts",
        "scenarios",
        ["added_scenario_id"],
        ["id"],
    )

    op.drop_constraint("ck_discovery_drafts_source_valid", "discovery_drafts", type_="check")

    op.drop_constraint(op.f("pk_discovery_drafts"), "discovery_drafts", type_="primary")
    op.create_primary_key(op.f("pk_discovery_drafts"), "discovery_drafts", ["run_id", "draft_id"])

    op.alter_column("discovery_drafts", "run_id", existing_type=sa.UUID(), nullable=False)
    op.drop_column("discovery_drafts", "updated_at")
    op.drop_column("discovery_drafts", "created_at")
    op.drop_column("discovery_drafts", "proposed_metrics")
    op.drop_column("discovery_drafts", "script")
    op.drop_column("discovery_drafts", "goal")
    op.drop_column("discovery_drafts", "persona_id")
    op.drop_column("discovery_drafts", "source")
    op.drop_column("discovery_drafts", "suite_id")

    op.drop_column("agents", "description")
    op.drop_column("agents", "prompt")
