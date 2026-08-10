"""004_run_recording_url

Revision ID: a350430ed7e9
Revises: 8b846bdde9ea
Create Date: 2026-08-10 16:02:11.238241

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a350430ed7e9"
down_revision: str | Sequence[str] | None = "8b846bdde9ea"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Nullable, populated only for simulation runs whose caller actually
    # started a room recording (app.engine.caller.recording) -- absent when
    # RECORDING_GCS_BUCKET isn't configured, or for run types that don't go
    # through the real executor at all (FakeRunner-backed dashboard runs).
    op.add_column("runs", sa.Column("recording_url", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("runs", "recording_url")
