"""B2-05: typed shapes shared by the incremental and final judge passes.

`AssertionSpec` widens the scenario's raw `assertions` JSONB (currently just
`{"id", "name"}`, see app/schemas/suites.py) with the fields a judge actually
needs to evaluate a signal: `description` (what counts as passing) and
`distinguish_from` (house style per the spine -- clarifies what this signal
is NOT, to stop the judge confusing adjacent assertions). Widening the DB
schema itself is out of scope here; callers building specs from a scenario's
JSONB can default `distinguish_from` to "".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.runs import TranscriptTurn


@dataclass(frozen=True)
class AssertionSpec:
    id: str
    name: str
    description: str
    distinguish_from: str = ""


@dataclass(frozen=True)
class AssertionState:
    """An assertion's status as of the incremental judge's last verdict --
    the incremental prompt is given these so it only flips an assertion when
    the transcript newly resolves it, not re-litigating turn after turn."""

    assertion_id: str
    status: Literal["undetermined", "passed", "failed"]


@dataclass(frozen=True)
class CompiledMetricSignal:
    """B2.7-06: an `llm_judge`-kind resolved metric, reshaped into the same
    id/description/distinguish_from-from shape `AssertionSpec` already
    uses -- rendered into `final.jinja2` as an additional signal block,
    same Definition/Distinguish-from house style, batched into the *same*
    final-pass call rather than a separate one per metric (app.engine.
    metrics.resolver's `ResolvedMetric.spec` holds `description`/
    `distinguish_from` for every llm_judge builtin -- see
    app/engine/metrics/builtins.py)."""

    metric_id: str
    name: str
    description: str
    distinguish_from: str = ""


class AssertionFlip(BaseModel):
    """`analysis` is declared before the verdict fields deliberately: Gemini's
    structured output fills schema fields in declaration order, so this is
    the "mandatory analysis-before-JSON" house style (spine, judge/scorer)
    realized as an analysis-before-verdict field ordering within one
    structured call, rather than a separate free-text reasoning call."""

    assertion_id: str
    analysis: str
    status: Literal["passed", "failed"]
    turn_refs: list[int] = Field(min_length=1)
    rationale: str


class IncrementalVerdict(BaseModel):
    flips: list[AssertionFlip] = Field(default_factory=list)
    live_score: int = Field(ge=0, le=100)


class FinalAssertionNote(BaseModel):
    assertion_id: str
    analysis: str
    status: Literal["passed", "failed"]
    turn_refs: list[int] = Field(min_length=1)
    note: str


class MetricVerdict(BaseModel):
    """`value` is always a string on the wire, never a `float | str | bool`
    union: Gemini's structured-output support (`response_schema=`) is
    unreliable against Pydantic unions, so every metric kind (numeric,
    boolean, tri_state, enum) reports its value as text here, and
    app.engine.metrics.compiler is what coerces it into the right shape
    for MetricResult once this comes back -- never the judge's own
    response schema."""

    metric_id: str
    analysis: str
    status: Literal["passed", "failed", "warn"]
    value: str
    turn_refs: list[int] = Field(min_length=1)
    rationale: str


class FinalVerdict(BaseModel):
    final_score: int = Field(ge=0, le=100)
    assertions: list[FinalAssertionNote]
    # B2.7-06: empty by default so every existing call site (evaluate()
    # with no metrics, every golden-eval case) is unaffected -- see
    # FinalJudge.evaluate's own optional `metrics` kwarg.
    metrics: list[MetricVerdict] = Field(default_factory=list)
    # B2.7-09: the third, distinct tier -- one holistic judgment, not a
    # list. None when no goal was given to evaluate (evaluate()'s `goal`
    # kwarg is None -- most scenarios today, and every golden-eval case,
    # which carry no goal field at all). `goal_analysis` is declared before
    # `goal_met` for the same analysis-before-verdict reason `analysis`
    # precedes `status` on AssertionFlip/FinalAssertionNote/MetricVerdict.
    goal_analysis: str | None = None
    goal_met: bool | None = None
    goal_turn_refs: list[int] = Field(default_factory=list)
    sentiment: Literal["positive", "neutral", "negative"]
    summary: str


__all__ = [
    "AssertionFlip",
    "AssertionSpec",
    "AssertionState",
    "CompiledMetricSignal",
    "FinalAssertionNote",
    "FinalVerdict",
    "IncrementalVerdict",
    "MetricVerdict",
    "TranscriptTurn",
]
