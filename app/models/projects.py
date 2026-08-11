import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, uuid_pk
from app.models.organizations import DEFAULT_ORG_ID

# Deterministic id for the single seeded default project (B2-06): the
# migration inserts this exact row and every existing agents/suites/runs
# row is backfilled to it, so application code and the migration must agree
# on the id without a runtime lookup -- same pattern as app/seed.py's
# uuid5-of-a-stable-key ids.
DEFAULT_PROJECT_ID = uuid.uuid5(uuid.NAMESPACE_DNS, "cadence.project.default")


class Project(Base, TimestampMixin):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = uuid_pk()
    # B2.5-02: a real FK, not OrgScopedMixin's hardcoded Text column --
    # Project is the one table where org_id is a genuine, enforced scope
    # rather than a placeholder for a future multi-tenancy migration (see
    # OrgScopedMixin's docstring; every other table stays on that mixin and
    # scopes transitively through project_id, per Cadence_stitch_protocol.md
    # §5's additive-or-constraint-only rule).
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False, server_default=str(DEFAULT_ORG_ID)
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # B2.5-01: soft delete -- DELETE /v1/projects refuses (409) while the
    # project is non-empty, and a deleted project must keep existing
    # historical runs/suites/agents resolvable rather than cascading. Every
    # list/lookup filters this out; nothing hard-deletes a project row.
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)
