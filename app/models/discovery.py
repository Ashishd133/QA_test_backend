import uuid

from sqlalchemy import CheckConstraint, Float, ForeignKey, ForeignKeyConstraint, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, OrgScopedMixin, TimestampMixin


class DiscoveryNode(Base, OrgScopedMixin):
    __tablename__ = "discovery_nodes"
    __table_args__ = (
        # Same guarantee as findings.evidence, applied to discovery (spine §3):
        # the gate's human-readable explanation cannot be silently dropped.
        CheckConstraint(
            "state <> 'blocked' OR blocked_reason IS NOT NULL", name="blocked_reason_required"
        ),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    node_id: Mapped[str] = mapped_column(Text, primary_key=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    x: Mapped[float] = mapped_column(Float, nullable=False)
    y: Mapped[float] = mapped_column(Float, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class DiscoveryEdge(Base, OrgScopedMixin):
    __tablename__ = "discovery_edges"
    __table_args__ = (
        # Named explicitly: the naming convention derives "%(column_0_name)s"
        # from the first column in each list, which is `run_id` for both —
        # left to the convention, these two collide on the same generated name.
        ForeignKeyConstraint(
            ["run_id", "from_node"],
            ["discovery_nodes.run_id", "discovery_nodes.node_id"],
            name="fk_discovery_edges_from_node_discovery_nodes",
        ),
        ForeignKeyConstraint(
            ["run_id", "to_node"],
            ["discovery_nodes.run_id", "discovery_nodes.node_id"],
            name="fk_discovery_edges_to_node_discovery_nodes",
        ),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    from_node: Mapped[str] = mapped_column(Text, primary_key=True)
    to_node: Mapped[str] = mapped_column(Text, primary_key=True)


class DiscoveryIntent(Base, OrgScopedMixin):
    __tablename__ = "discovery_intents"
    __table_args__ = (
        CheckConstraint("state <> 'blocked' OR reason IS NOT NULL", name="reason_required"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class DiscoveryDraft(Base, OrgScopedMixin, TimestampMixin):
    """B2.7-12: draft_id alone is the PK now (B1-01's from-draft acceptance
    already assumed it was globally addressable -- this makes that true
    rather than documenting the gap). `run_id` is nullable because a
    `source='agent_prompt'`/`'agent_description'` draft has no discovery
    run behind it; only `source='discovery_run'` populates it. `suite_id`
    replaces run_id as what scopes a draft to a project (via its suite),
    matching how a real scenario scopes (B2.5-01's comment on
    `_fetch_scenario_project_or_404`)."""

    __tablename__ = "discovery_drafts"
    __table_args__ = (
        CheckConstraint(
            "source IN ('agent_prompt', 'agent_description', 'discovery_run')",
            name="source_valid",
        ),
    )

    draft_id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    suite_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("suites.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # Free-text persona suggestion from the LLM -- app.api.suites._resolve_
    # persona name-matches this against visible personas at accept time
    # (same as a discovery-sourced draft always has); persona_id is the
    # already-resolved id generation itself found a match for, so accept
    # doesn't have to re-resolve when it's present.
    persona: Mapped[str] = mapped_column(Text, nullable=False)
    persona_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("personas.id"), nullable=True)
    goal: Mapped[str | None] = mapped_column(Text, nullable=True)
    script: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    assertions: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    # [{"metricId": str}, ...] -- resolved metric ids only (unresolvable
    # names are dropped at generation time, never stored unresolvable).
    proposed_metrics: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    added_scenario_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("scenarios.id", ondelete="SET NULL"), nullable=True
    )
