import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, OrgScopedMixin, TimestampMixin, uuid_pk


class Agent(Base, OrgScopedMixin, TimestampMixin):
    __tablename__ = "agents"
    __table_args__ = (
        CheckConstraint("transport IN ('web', 'sip', 'phone')", name="transport_valid"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    transport: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, server_default="{}")
    language: Mapped[str | None] = mapped_column(Text, nullable=True)
    max_concurrency: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    status: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_by_user_id: Mapped[str] = mapped_column(Text, nullable=False)
