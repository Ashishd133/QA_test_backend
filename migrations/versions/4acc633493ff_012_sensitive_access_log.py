"""012_sensitive_access_log

Revision ID: 4acc633493ff
Revises: 52bb225d68a4
Create Date: 2026-09-27 16:36:12.816260

B2.7-03: one generic audit table for every encrypted-at-rest resource
(test_profiles now, B4-05's findings.evidence later appends
resource_type='finding' rows here rather than getting a second table).
Written in the same transaction as the decrypting read it records.

Autogenerate again proposed dropping the same five pre-existing indexes
noted in migration 010's docstring (ix_agents_project_id and friends) --
still a model/DB drift unrelated to this ticket, still left alone.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4acc633493ff"
down_revision: str | Sequence[str] | None = "52bb225d68a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "sensitive_access_log",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("resource_type", sa.Text(), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column(
            "at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sensitive_access_log")),
    )
    op.create_index(
        "ix_sensitive_access_log_resource",
        "sensitive_access_log",
        ["resource_type", "resource_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_sensitive_access_log_resource", table_name="sensitive_access_log")
    op.drop_table("sensitive_access_log")
