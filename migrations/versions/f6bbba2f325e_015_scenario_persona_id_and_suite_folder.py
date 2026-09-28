"""015_scenario_persona_id_and_suite_folder

Revision ID: f6bbba2f325e
Revises: a08eae7ae4cd
Create Date: 2026-09-28 19:25:29.824038

B2.7-11, expand phase of a two-migration column drop. `scenarios.persona`/
`persona_initials` were supposed to be dual-written alongside `persona_id`
per B2.7-01's plan (migration 010) -- turns out that never actually
happened in any write path (checked: `app/api/suites.py`'s `add_scenario`
and `app/seed.py`'s scenario insert both only ever wrote the text columns),
so 010's one-time backfill is the *only* thing that has ever set
`persona_id`, and every scenario created since then has NULL `persona_id`
(no voice/language/accent reaches the engine for those calls).

This migration is safe to run while old code (which still reads/writes
`persona`/`persona_initials` and ignores `persona_id`) is live, because it
only adds/loosens, never removes:
  1. Insert a builtin persona row for "Asha Rao" (app.engine.caller.
     persona_call.CARD_BLOCK_PERSONA) -- the one scenario from migration
     010's original backfill that had no matching personas row to link,
     since it predates the personas table. Deterministic id via the same
     uuid5(uuid5(NAMESPACE_DNS, "cadence.seed"), "persona:asha") app.seed
     will use, so seed.py's own ON CONFLICT DO NOTHING insert coincides
     with this row rather than duplicating it.
  2. Backfill `scenarios.persona_id` for every row where it's still NULL,
     by exact (case-insensitive) name match against `personas.name` --
     scoped to the scenario's own project (via its suite) or a builtin
     persona, specifically to avoid creating a cross-project reference via
     a same-named persona in an unrelated project.
  3. `persona`/`persona_initials` become nullable (still present, just no
     longer required) -- new code (this same commit) stops writing them
     entirely, so nothing downstream depends on them past this point.
  4. `suites.folder`, purely additive.

Migration 016 (contract phase, not in this commit) re-runs the same
backfill (idempotent, catches anything old code inserted in the gap before
it's retired everywhere), asserts zero remaining NULL `persona_id`, sets it
NOT NULL, and drops both legacy columns. It runs against `TEST_DATABASE_URL`
immediately; against `DATABASE_URL` only once the live Railway services are
confirmed running the new code (see B2.7_DEFERRED_FOLLOWUPS.md).

Autogenerate again proposed dropping the same 5 pre-existing spurious
indexes noted since migration 010's docstring (ix_agents_project_id and
friends) -- still unrelated drift, still left alone.
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f6bbba2f325e"
down_revision: str | Sequence[str] | None = "a08eae7ae4cd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Same computation as app.seed's _id("persona:asha") -- see this module's
# docstring for why the two need to coincide.
_ASHA_RAO_PERSONA_ID = uuid.uuid5(uuid.uuid5(uuid.NAMESPACE_DNS, "cadence.seed"), "persona:asha")

_BACKFILL_PERSONA_ID_SQL = sa.text(
    "UPDATE scenarios sc SET persona_id = p.id "
    "FROM suites s, personas p "
    "WHERE sc.suite_id = s.id "
    "  AND sc.persona_id IS NULL "
    "  AND lower(p.name) = lower(sc.persona) "
    "  AND (p.project_id IS NULL OR p.project_id = s.project_id)"
)


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column("scenarios", "persona", existing_type=sa.TEXT(), nullable=True)
    op.alter_column("scenarios", "persona_initials", existing_type=sa.TEXT(), nullable=True)
    op.add_column("suites", sa.Column("folder", sa.Text(), nullable=True))

    op.execute(
        sa.text(
            "INSERT INTO personas (id, name, voice, language, accent, traits, builtin) "
            "VALUES (:id, 'Asha Rao', 'en-US-Chirp3-HD-Charon', 'en-US', NULL, "
            " CAST(:traits AS jsonb), true) "
            "ON CONFLICT (id) DO NOTHING"
        ).bindparams(id=_ASHA_RAO_PERSONA_ID, traits='{"tone": "polite", "patience": "medium"}')
    )
    op.execute(_BACKFILL_PERSONA_ID_SQL)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("suites", "folder")
    op.alter_column("scenarios", "persona_initials", existing_type=sa.TEXT(), nullable=False)
    op.alter_column("scenarios", "persona", existing_type=sa.TEXT(), nullable=False)
