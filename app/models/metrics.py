import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, OrgScopedMixin, TimestampMixin, uuid_pk


class Metric(Base, OrgScopedMixin, TimestampMixin):
    __tablename__ = "metrics"
    __table_args__ = (
        CheckConstraint("kind IN ('builtin', 'llm_judge', 'python')", name="kind_valid"),
        CheckConstraint(
            "output_type IN ('boolean', 'numeric', 'enum', 'tri_state')", name="output_type_valid"
        ),
        CheckConstraint("status IN ('draft', 'active', 'archived')", name="status_valid"),
        # Two partial indexes, not one plain UNIQUE(project_id, agent_id,
        # name): agent_id is nullable (project-level metric), and Postgres
        # treats NULLs as distinct from each other in a plain unique
        # constraint -- a naive UNIQUE(project_id, agent_id, name) would
        # silently allow two different project-level "X" metrics to
        # coexist, which is exactly the ambiguity B2.7-04's precedence rule
        # ("agent-level overrides project-level of the same name") can't
        # tolerate. This still lets a project-level "X" and an agent-level
        # "X" coexist by design -- they're the override pair, not a
        # collision -- since they land in different indexes.
        Index(
            "uq_metrics_project_agent_name",
            "project_id",
            "agent_id",
            "name",
            unique=True,
            postgresql_where="agent_id IS NOT NULL",
        ),
        Index(
            "uq_metrics_project_name_no_agent",
            "project_id",
            "name",
            unique=True,
            postgresql_where="agent_id IS NULL",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), nullable=False)
    # NULL = project-level metric (the floor every agent in the project
    # inherits); a real value = agent-level override of the project metric
    # with the same name, per B2.7-04's resolution order.
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    output_type: Mapped[str] = mapped_column(Text, nullable=False)
    # prompt | code | builtin key + threshold, shape depends on `kind` --
    # deliberately not modeled as separate columns, since the three kinds'
    # specs share almost nothing (B2.7-06 is the one place that interprets
    # this by kind).
    spec: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, server_default="{}")
    sampling_pct: Mapped[int] = mapped_column(Integer, nullable=False, server_default="100")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft")
    # B2.7-04: PATCH on an active metric bumps this and does NOT retroactively
    # rescore -- historical assertion_results/verdicts keep the version that
    # produced them, so this is never decremented or reused.
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_by_user_id: Mapped[str] = mapped_column(Text, nullable=False)


class ScenarioMetric(Base):
    """Join table: which metrics apply to a scenario, and whether each one
    gates the run's pass/fail verdict (B2.7-08) or is informational only.
    No surrogate id -- the pair *is* the identity, and there's nothing else
    to reference it by."""

    __tablename__ = "scenario_metrics"

    scenario_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scenarios.id", ondelete="CASCADE"), primary_key=True
    )
    metric_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("metrics.id", ondelete="CASCADE"), primary_key=True
    )
    gating: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
