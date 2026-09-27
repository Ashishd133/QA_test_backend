import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
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
        # B2.7-04: a built-in metric (builtin=true, same shape as
        # personas.builtin) has project_id/agent_id both NULL and is
        # read-only project-wide -- the resolution floor every scenario
        # gets before any project/agent override. Keeping `builtin` as its
        # own column (not just inferring it from project_id IS NULL) mirrors
        # personas.py exactly and keeps this CHECK a single, readable
        # invariant rather than three nullability rules a caller has to
        # reconstruct by hand.
        CheckConstraint(
            "(builtin AND project_id IS NULL AND agent_id IS NULL) "
            "OR (NOT builtin AND project_id IS NOT NULL)",
            name="builtin_scope_valid",
        ),
        # Three partial indexes, not one plain UNIQUE(project_id, agent_id,
        # name): agent_id is nullable (project-level metric) and project_id
        # is nullable too now (builtin), and Postgres treats NULLs as
        # distinct from each other in a plain unique constraint -- a naive
        # UNIQUE(project_id, agent_id, name) would silently allow two
        # different project-level "X" metrics to coexist, which is exactly
        # the ambiguity B2.7-04's precedence rule ("agent-level overrides
        # project-level overrides builtin, same name") can't tolerate. A
        # project-level "X", an agent-level "X", and a builtin "X" can all
        # coexist by design -- that's the override chain, not a collision --
        # since each lands in its own index.
        Index(
            "uq_metrics_builtin_name",
            "name",
            unique=True,
            postgresql_where="builtin",
        ),
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
            postgresql_where="NOT builtin AND agent_id IS NULL",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    # Nullable, unlike every other project_id -- NULL for a built-in metric
    # (builtin=true), same convention as personas.project_id. Deliberately
    # no server_default: the app always sets this explicitly, never falls
    # back to a real project by accident.
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    # NULL = project-level metric (the floor every agent in the project
    # inherits, above the builtin floor); a real value = agent-level
    # override of the project metric with the same name, per B2.7-04's
    # resolution order.
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
    # rescore -- historical metric_results keep the version that produced
    # them, so this is never decremented or reused.
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_by_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")


class MetricResult(Base, OrgScopedMixin):
    """One row per (run, metric): the metric's final, materialized verdict
    for that run -- same shape as AssertionResult (app/models/runs.py), not
    a per-turn log. `turn_refs` cites which turns the verdict is based on
    (mirrors Finding.turn_refs' ARRAY(Integer), not a jsonb blob, for the
    same reason: it's always a flat list of ints, nothing more).

    No ON DELETE behavior on `metric_id` and no denormalized copy of the
    metric's name/kind here: DELETE /v1/metrics/{id} 409s once any
    metric_results row references it (same "in use" conflict shape as
    personas.py), so this FK never needs to tolerate a vanished metric --
    current name/kind/output_type are always one JOIN away.
    """

    __tablename__ = "metric_results"
    __table_args__ = (
        CheckConstraint(
            "status IN ('passed', 'failed', 'warn', 'error', 'not_sampled', 'not_computable')",
            name="status_valid",
        ),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    metric_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("metrics.id"), primary_key=True)
    # Stamped at scoring time -- a later PATCH bumping the live metric's
    # `version` must never change what this row says produced it (B2.7-04's
    # own "editing a metric leaves prior results untouched and
    # version-stamped" bar).
    metric_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    turn_refs: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), nullable=False, server_default="{}"
    )
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)


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
