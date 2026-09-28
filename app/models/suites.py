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
    # B2.7-01/08: {gatingMetricIds[], minPassRate, failOnAnyCritical}. NULL,
    # not an empty object -- "no rubric configured" is a real, distinct state
    # (everything informational, nothing gates the run) from "a rubric that
    # gates on nothing," and B2.7-08's evaluator has to handle the NULL case
    # either way, so it isn't hidden behind a default.
    rubric: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    # B2.7-11: a flat text label, not a table -- "resist the tree" per the
    # ticket. NULL means "no folder" (the default, top-level state), not the
    # empty string.
    folder: Mapped[str | None] = mapped_column(Text, nullable=True)


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
    script: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    assertions: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_draft_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    # B2.7-09: the Goal tier, judged holistically by the final pass --
    # distinct from `assertions` (many, per-turn, incremental) and from
    # scenario_metrics (many, scored, not binary). NULL for scenarios
    # authored before this ticket; goalMet has nothing to evaluate for them.
    goal: Mapped[str | None] = mapped_column(Text, nullable=True)
    # B2.7-01/11: the sole source of a scenario's persona as of migration
    # 016 -- the original `persona`/`persona_initials` text dual-write
    # columns are gone (see migration 015's docstring for why they were
    # ever nullable, and 016's for the backfill that preceded the drop).
    persona_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("personas.id"), nullable=False)
    # SET NULL, not CASCADE/RESTRICT: a deleted test profile shouldn't take
    # the scenario down with it, just drop the reference (same reasoning as
    # runs.scenario_id's ondelete in migration 002).
    test_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("test_profiles.id", ondelete="SET NULL"), nullable=True
    )
    # E1 (condition & fault injection) carrier -- engine-wired later, schema
    # exists now so E1 is engine work only, not a migration.
    conditions: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
