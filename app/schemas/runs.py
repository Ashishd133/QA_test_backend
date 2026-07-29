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


class DummyIdentity(APIModel):
    """Deliberately loose (plain strs, no field validators): spine §6 --
    only *format* is checked at POST time (app/api/runs.py's
    _validate_dummy_identity, raising the specific `invalid_identity` code
    the ticket names, not Pydantic's generic 422). A valid-format-but-wrong
    identity is not an error; the run proceeds and gated branches come back
    `blocked` with a reason once discovery is actually implemented (B5)."""

    name: str
    dob: str
    account: str
    verification_phrase: str


class SimulationRunCreate(APIModel):
    scenario_id: str


AttackCategoryKey = Literal["Injection", "PII", "Auth", "Social", "Harmful"]


class DiscoveryRunCreate(APIModel):
    agent_id: str
    dummy_identity: DummyIdentity


class RedteamRunCreate(APIModel):
    agent_id: str
    categories: list[AttackCategoryKey]


class RunCreateResponse(APIModel):
    run_id: str
