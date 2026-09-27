import uuid

from sqlalchemy import Boolean, Float, ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, OrgScopedMixin, TimestampMixin, uuid_pk


class Persona(Base, OrgScopedMixin, TimestampMixin):
    __tablename__ = "personas"

    id: Mapped[uuid.UUID] = uuid_pk()
    # B2.7-01: nullable, unlike every other project_id -- a built-in persona
    # (builtin=true) has project_id IS NULL and is read-only project-wide;
    # "duplicate to project" (B2.7-02) is how a user gets an editable copy
    # with a real project_id. Deliberately no server_default: the app always
    # sets this explicitly (a real value for a user-authored persona, left
    # NULL for a built-in), never falls back to DEFAULT_PROJECT_ID by accident.
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    voice: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(Text, nullable=False)
    accent: Mapped[str | None] = mapped_column(Text, nullable=True)
    traits: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, server_default="{}")
    builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # B2.7-02: the fuller condition-field set. Only language/accent/voice/
    # emotion are wired to the engine as of this ticket -- interruption_
    # behavior/environment/code_switch are carriers for epics E1/E2, added
    # now because the column costs nothing today and a backfill plus UI
    # rework later would not be free.
    emotion: Mapped[str | None] = mapped_column(Text, nullable=True)
    speaking_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    interruption_behavior: Mapped[str | None] = mapped_column(Text, nullable=True)
    environment: Mapped[str | None] = mapped_column(Text, nullable=True)
    code_switch: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
