import uuid

from sqlalchemy import Boolean, ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, OrgScopedMixin, TimestampMixin, uuid_pk


class TestProfile(Base, OrgScopedMixin, TimestampMixin):
    """B2.7-03: named, reusable field sets a scenario can attach (the thing
    Discovery's `dummyIdentity` -- currently inline in `runs.config` -- is
    migrating to reference, so encryption has one code path instead of two).
    `fields` holds ciphertext when `encrypted` is true (app.gcp_auth-adjacent
    key management, access-logged on read) -- this table only knows it's an
    opaque JSON blob, never the plaintext shape."""

    __tablename__ = "test_profiles"

    id: Mapped[uuid.UUID] = uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    fields: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, server_default="{}")
    encrypted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_by_user_id: Mapped[str] = mapped_column(Text, nullable=False)
