import uuid

from sqlalchemy import CheckConstraint, Float, ForeignKey, ForeignKeyConstraint, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, OrgScopedMixin


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


class DiscoveryDraft(Base, OrgScopedMixin):
    __tablename__ = "discovery_drafts"

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    draft_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    persona: Mapped[str] = mapped_column(Text, nullable=False)
    assertions: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    added_scenario_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("scenarios.id"), nullable=True
    )
