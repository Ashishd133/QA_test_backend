import uuid
from datetime import datetime

from sqlalchemy import DateTime, MetaData, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Explicit constraint names so `alembic revision --autogenerate` produces
# stable, deterministic diffs instead of Postgres-assigned names that vary
# (same discipline as the OpenAPI export in scripts/export_openapi.py).
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    # Every Mapped[datetime] column is TIMESTAMPTZ, not naive TIMESTAMP — the
    # reaper compares heartbeat_at across worker processes (spine §5), and
    # naive timestamps make that comparison silently wrong (or asyncpg raises
    # on encode) the moment anything writes tz-aware datetimes.
    type_annotation_map = {datetime: DateTime(timezone=True)}


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, default=uuid.uuid4)


class OrgScopedMixin:
    """org_id on every table from day one (spine §3): hardcoded to one org
    for MVP, so multi-tenancy later is a WHERE clause, not a migration."""

    org_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="org_default")


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )
