import uuid

from sqlalchemy import Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, OrgScopedMixin, TimestampMixin, uuid_pk

# Deterministic id for the single seeded default project (B2-06): the
# migration inserts this exact row and every existing agents/suites/runs
# row is backfilled to it, so application code and the migration must agree
# on the id without a runtime lookup -- same pattern as app/seed.py's
# uuid5-of-a-stable-key ids.
DEFAULT_PROJECT_ID = uuid.uuid5(uuid.NAMESPACE_DNS, "cadence.project.default")


class Project(Base, OrgScopedMixin, TimestampMixin):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
