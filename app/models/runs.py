import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, OrgScopedMixin, TimestampMixin, uuid_pk


class Run(Base, OrgScopedMixin, TimestampMixin):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("idempotency_key"),
        CheckConstraint(
            "type IN ('simulation', 'discovery', 'redteam', 'suite')", name="type_valid"
        ),
        CheckConstraint(
            "status IN ('queued', 'claimed', 'running', 'completed', 'cancelled', 'failed')",
            name="status_valid",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id"), nullable=False)
    scenario_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("scenarios.id", ondelete="SET NULL"), nullable=True
    )
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    config: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, server_default="{}")
    idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    claimed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(nullable=True)
    metrics: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)


class RunEvent(Base, OrgScopedMixin):
    """Append-only spine (spine §3/§4/Amendment A) — the ONLY write path is
    app.events.emit(); SSE, replay, and debugging all read this table."""

    __tablename__ = "run_events"

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    ts: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())


class Turn(Base, OrgScopedMixin):
    """Materialized at run completion from run_events (spine §5/B1-07) —
    events remain the source of truth; this is for query/report convenience."""

    __tablename__ = "turns"
    __table_args__ = (CheckConstraint("role IN ('caller', 'agent')", name="role_valid"),)

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    idx: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    flagged: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    flag_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class AssertionResult(Base, OrgScopedMixin):
    __tablename__ = "assertion_results"
    __table_args__ = (CheckConstraint("status IN ('passed', 'failed')", name="status_valid"),)

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    assertion_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    triggered_at_turn: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Finding(Base, OrgScopedMixin, TimestampMixin):
    __tablename__ = "findings"
    __table_args__ = (
        CheckConstraint("verdict IN ('blocked', 'leaked', 'bypassed')", name="verdict_valid"),
        # Product logic in the schema (spine §3): a leak/bypass without a
        # verbatim evidence quote must be impossible to persist.
        CheckConstraint("verdict = 'blocked' OR evidence IS NOT NULL", name="evidence_required"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"), nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    attack_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    agent_response: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggested_fix: Mapped[str] = mapped_column(Text, nullable=False)
    turn_refs: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), nullable=False, server_default="{}"
    )
