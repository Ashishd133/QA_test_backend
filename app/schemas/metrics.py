"""Request/response models for /v1/metrics (B2.7-04)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class APIModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class MetricDetail(APIModel):
    id: str
    # None = built-in (project-wide, read-only, the resolution floor); a
    # real value = project-level (agent_id None) or agent-level metric.
    project_id: str | None = None
    agent_id: str | None = None
    name: str
    description: str
    kind: Literal["builtin", "llm_judge", "python"]
    output_type: Literal["boolean", "numeric", "enum", "tri_state"]
    spec: dict[str, object]
    sampling_pct: int
    status: Literal["draft", "active", "archived"]
    version: int
    builtin: bool


class MetricCreate(APIModel):
    name: str
    description: str = ""
    kind: Literal["builtin", "llm_judge", "python"]
    output_type: Literal["boolean", "numeric", "enum", "tri_state"]
    spec: dict[str, object] = {}
    sampling_pct: int = 100
    # Omitted = project-level metric; a real value = an agent-level
    # override of the project (or builtin) metric with the same name.
    agent_id: str | None = None


class MetricUpdate(APIModel):
    name: str | None = None
    description: str | None = None
    spec: dict[str, object] | None = None
    sampling_pct: int | None = None
    status: Literal["draft", "active", "archived"] | None = None


class BacktestRequest(APIModel):
    sample_size: int = 40
    # Currently supports agentId -- the ticket's own "filters" is loose by
    # design (a dict, not a fixed schema) since the exact filter set is
    # expected to grow; app.engine.metrics.backtest documents which keys
    # it actually reads.
    filters: dict[str, object] = {}


class BacktestCallVerdict(APIModel):
    run_id: str
    status: Literal["passed", "failed", "warn", "error", "not_computable"]
    value: str | float | bool | None = None
    turn_refs: list[int] = []
    rationale: str | None = None


class BacktestResult(APIModel):
    id: str
    verdicts: list[BacktestCallVerdict]
    # B2.7-10: null/0 until this codebase has a human-label store to
    # compare against -- honest, not a stub value (see MetricBacktest's
    # own docstring).
    agreement: float | None = None
    labeled_count: int = 0
    cost: dict[str, object]


class ResolvedMetric(APIModel):
    """The effective metric set for one scenario -- one row per unique
    `name`, already resolved through the agent > project > builtin
    precedence chain. `source_level` says which one won, so the UI (or a
    debugging human) never has to re-derive precedence from raw rows."""

    id: str
    name: str
    kind: Literal["builtin", "llm_judge", "python"]
    output_type: Literal["boolean", "numeric", "enum", "tri_state"]
    spec: dict[str, object]
    sampling_pct: int
    version: int
    source_level: Literal["builtin", "project", "agent"]
