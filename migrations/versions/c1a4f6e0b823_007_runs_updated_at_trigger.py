"""007_runs_updated_at_trigger

Revision ID: c1a4f6e0b823
Revises: b7d4e2a91c05
Create Date: 2026-08-11 00:00:00.000000

B2.5-05: `runs.updated_at`'s `onupdate=func.now()` (TimestampMixin) is a
SQLAlchemy Core/ORM-side default -- it only fires through Core's
`Table.update()` or an ORM flush. Every write in this codebase is a raw
`text("UPDATE runs SET ...")` statement (cancel, claim, materialize,
rerun, ...), which bypasses that machinery entirely, so `updated_at` has
never actually moved on any status transition. B2.5-05's delta endpoint
needs `updated_at` as a real "last changed" signal for its `since=` filter
and ETag; a `BEFORE UPDATE` trigger is a DB-level guarantee that holds
regardless of which code path (raw SQL today, ORM tomorrow) does the
write. Scoped to `runs` only -- the one table this ticket depends on.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1a4f6e0b823"
down_revision: str | Sequence[str] | None = "b7d4e2a91c05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FUNCTION_NAME = "set_updated_at"
_TRIGGER_NAME = "runs_set_updated_at"


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        f"CREATE OR REPLACE FUNCTION {_FUNCTION_NAME}() RETURNS trigger AS $$ "
        "BEGIN NEW.updated_at = now(); RETURN NEW; END; "
        "$$ LANGUAGE plpgsql"
    )
    op.execute(
        f"CREATE TRIGGER {_TRIGGER_NAME} BEFORE UPDATE ON runs "
        f"FOR EACH ROW EXECUTE FUNCTION {_FUNCTION_NAME}()"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER_NAME} ON runs")
    op.execute(f"DROP FUNCTION IF EXISTS {_FUNCTION_NAME}()")
