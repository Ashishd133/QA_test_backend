"""Request/response models for GET /v1/runs (B1-03).

Run/DashboardRunRow/TranscriptTurn/ResultAssertion match the frontend's
src/types/index.ts verbatim. Detail is assembled by reducing run_events
directly (spine: events remain the truth) rather than reading
turns/assertion_results -- those materialized tables don't exist until
B1-07, and reducing from events works whether or not that ticket has run
yet (B1-07's own "done when" proves both paths must agree).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.verdict import Verdict


class APIModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class TranscriptTurn(APIModel):
    role: Literal["caller", "agent"]
    text: str
    lat: str | None = None
    flag: bool | None = None
    flag_text: str | None = None


class ResultAssertion(APIModel):
    text: str
    detail: str
    ok: bool


class DashboardRunRow(APIModel):
    id: str
    suite: str
    agent: str
    status: Verdict
    pass_rate: str
    duration: str
    run_id: str


class RunDetail(APIModel):
    id: str
    title: str
    suite_name: str
    scenario_name: str
    agent_name: str
    agent_meta: str
    status: Verdict
    score: float
    avg_latency: str
    wer: str
    sentiment: str
    duration: str
    created_at: str
    transcript: list[TranscriptTurn]
    result_assertions: list[ResultAssertion]
    latency_series: list[float]
