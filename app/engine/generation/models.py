"""B2.7-12: typed shapes for LLM-generated scenario drafts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class GeneratedAssertion(BaseModel):
    id: str
    name: str
    description: str
    distinguish_from: str = ""


class GeneratedDraft(BaseModel):
    """`kind` is declared so the prompt can be told explicitly to produce
    at least one 'negative' draft (a case the agent should refuse, push
    back on, or require extra verification for) rather than leaving that
    to chance -- enforced after the call
    (app.api.suites.generate_scenarios), never trusted as a hard guarantee
    from the model alone.

    `scenario_goal` (the judge's holistic success criterion -> scenarios.
    goal) and `caller_goal` (the synthetic caller's own instruction for
    what to say/ask -> script.goal) are named distinctly for the same
    reason app.engine.executor.simulation._load_scenario's docstring
    gives: conflating the two is exactly how B2.7-09's two tiers would
    get silently merged back into one."""

    name: str
    kind: Literal["positive", "negative"]
    persona_name: str
    scenario_goal: str
    caller_goal: str
    opening_line: str
    assertions: list[GeneratedAssertion] = Field(min_length=1)
    metric_names: list[str] = Field(default_factory=list)


class GenerationResponse(BaseModel):
    drafts: list[GeneratedDraft]


__all__ = ["GeneratedAssertion", "GeneratedDraft", "GenerationResponse"]
