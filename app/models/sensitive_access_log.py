import uuid
from datetime import datetime

from sqlalchemy import Index, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, uuid_pk


class SensitiveAccessLog(Base):
    """B2.7-03: one generic audit table for every encrypted-at-rest
    resource, not one per resource type -- B4-05 appends `resource_type=
    "finding"` rows here instead of standing up a second table. Written in
    the same transaction as the decrypting read it records (app/api/
    test_profiles.py's get_test_profile), so there's no window where a
    read succeeds but its audit trail doesn't land."""

    __tablename__ = "sensitive_access_log"
    __table_args__ = (Index("ix_sensitive_access_log_resource", "resource_type", "resource_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    resource_type: Mapped[str] = mapped_column(Text, nullable=False)
    resource_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
