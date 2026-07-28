import uuid

from sqlalchemy import Boolean, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, OrgScopedMixin, TimestampMixin, uuid_pk


class Persona(Base, OrgScopedMixin, TimestampMixin):
    __tablename__ = "personas"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    voice: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(Text, nullable=False)
    accent: Mapped[str | None] = mapped_column(Text, nullable=True)
    traits: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, server_default="{}")
    builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
