import uuid

from sqlalchemy import CheckConstraint, ForeignKey, PrimaryKeyConstraint, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, uuid_pk

# Deterministic id for the single seeded default org (B2.5-02), same
# uuid5-of-a-stable-key pattern as app/models/projects.py's
# DEFAULT_PROJECT_ID -- the migration and the application need to agree on
# this id without a runtime lookup.
DEFAULT_ORG_ID = uuid.uuid5(uuid.NAMESPACE_DNS, "cadence.org.default")


class Organization(Base, TimestampMixin):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)


class OrgMembership(Base):
    """Stored but not enforced in B2.5-02 -- `role` carries no permission
    difference yet (that's E10's RBAC enforcement). What IS enforced here
    is membership itself: app.deps.require_project_id's org-membership
    check (added in this ticket) treats "no membership row" as "this org
    doesn't exist" for that caller, same 404-not-403 rule as project
    scoping."""

    __tablename__ = "org_memberships"
    __table_args__ = (
        PrimaryKeyConstraint("org_id", "user_id"),
        CheckConstraint("role IN ('admin', 'member', 'viewer')", name="role_valid"),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default="member")
