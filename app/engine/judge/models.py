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


class FinalVerdict(BaseModel):
    final_score: int = Field(ge=0, le=100)
    assertions: list[FinalAssertionNote]
    sentiment: Literal["positive", "neutral", "negative"]
    summary: str


__all__ = [
    "AssertionFlip",
    "AssertionSpec",
    "AssertionState",
    "FinalAssertionNote",
    "FinalVerdict",
    "IncrementalVerdict",
    "TranscriptTurn",
]
