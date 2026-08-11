import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, OrgScopedMixin, TimestampMixin, uuid_pk
from app.models.projects import DEFAULT_PROJECT_ID


class Suite(Base, OrgScopedMixin, TimestampMixin):
    __tablename__ = "suites"

    id: Mapped[uuid.UUID] = uuid_pk()
    # B2.5-01: see Agent.project_id's comment -- NOT NULL now.
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id"), nullable=False, server_default=str(DEFAULT_PROJECT_ID)
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id"), nullable=False)
    created_by_user_id: Mapped[str] = mapped_column(Text, nullable=False)


class Scenario(Base, OrgScopedMixin, TimestampMixin):
    __tablename__ = "scenarios"
    __table_args__ = (
        UniqueConstraint("source_draft_ref"),
        CheckConstraint("source IN ('manual', 'discovery_draft')", name="source_valid"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    suite_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("suites.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    persona: Mapped[str] = mapped_column(Text, nullable=False)
    persona_initials: Mapped[str] = mapped_column(Text, nullable=False)
    script: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    assertions: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_draft_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
