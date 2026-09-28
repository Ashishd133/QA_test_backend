"""016_drop_scenario_persona_text_columns

Revision ID: 888504255212
Revises: f6bbba2f325e
Create Date: 2026-09-28 19:38:29.605684

B2.7-11, contract phase. Companion to migration 015 (expand): re-runs the
same name-match backfill (idempotent -- a no-op for anything 015 already
caught, and catches only whatever old code inserted with a NULL
`persona_id` in the gap between 015 and this commit going live everywhere),
then asserts zero remaining NULL `persona_id` before setting it NOT NULL
and dropping `persona`/`persona_initials`. The assertion raises naming the
offending scenario ids rather than silently deleting or defaulting them --
an un-backfillable scenario is a data problem for a human to look at, not
something this migration should paper over.

Apply to TEST_DATABASE_URL immediately (no live reader). Apply to
DATABASE_URL only once the `api`/`worker` Railway services are confirmed
running code from this same commit or later (which stops reading/writing
the two dropped columns) -- see B2.7_DEFERRED_FOLLOWUPS.md.

Autogenerate again proposed dropping the same 5 pre-existing spurious
indexes noted since migration 010's docstring -- still unrelated drift,
still left alone.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "888504255212"
down_revision: str | Sequence[str] | None = "f6bbba2f325e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BACKFILL_PERSONA_ID_SQL = sa.text(
    "UPDATE scenarios sc SET persona_id = p.id "
    "FROM suites s, personas p "
    "WHERE sc.suite_id = s.id "
    "  AND sc.persona_id IS NULL "
    "  AND lower(p.name) = lower(sc.persona) "
    "  AND (p.project_id IS NULL OR p.project_id = s.project_id)"
)

_UNBACKFILLED_SQL = sa.text("SELECT id FROM scenarios WHERE persona_id IS NULL")


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()
    conn.execute(_BACKFILL_PERSONA_ID_SQL)
    remaining = conn.execute(_UNBACKFILLED_SQL).scalars().all()
    if remaining:
        raise RuntimeError(
            "cannot drop scenarios.persona/persona_initials: "
            f"{len(remaining)} scenario(s) still have no persona_id (ids: {remaining}) -- "
            "backfill or delete them first"
        )

    op.alter_column("scenarios", "persona_id", existing_type=sa.UUID(), nullable=False)
    op.drop_column("scenarios", "persona_initials")
    op.drop_column("scenarios", "persona")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column("scenarios", sa.Column("persona", sa.TEXT(), autoincrement=False, nullable=True))
    op.add_column(
        "scenarios", sa.Column("persona_initials", sa.TEXT(), autoincrement=False, nullable=True)
    )
    op.alter_column("scenarios", "persona_id", existing_type=sa.UUID(), nullable=True)
