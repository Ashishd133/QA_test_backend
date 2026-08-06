import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, OrgScopedMixin, TimestampMixin, uuid_pk
from app.models.projects import DEFAULT_PROJECT_ID


class Agent(Base, OrgScopedMixin, TimestampMixin):
    __tablename__ = "agents"
    __table_args__ = (
        CheckConstraint("transport IN ('web', 'sip', 'phone')", name="transport_valid"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    # B2-06: nullable (not required) -- server_default fills it for new
    # rows, existing rows are backfilled by the migration, but the column
    # itself stays optional (schema/plumbing only, per the ticket -- no
    # project switcher exists yet to make this a real user-facing choice).
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id"), nullable=True, server_default=str(DEFAULT_PROJECT_ID)
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    transport: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, server_default="{}")
    language: Mapped[str | None] = mapped_column(Text, nullable=True)
    max_concurrency: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    status: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_by_user_id: Mapped[str] = mapped_column(Text, nullable=False)
