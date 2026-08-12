"""Request/response models for GET /v1/runs (B1-03).

Run/DashboardRunRow/TranscriptTurn/ResultAssertion match the frontend's
src/types/index.ts verbatim. Detail is assembled by reducing run_events
directly (spine: events remain the truth) rather than reading
turns/assertion_results -- B1-07 materializes those tables at completion
for query/report convenience, but this read path deliberately keeps
reducing from events so it works identically for in-flight and completed
runs (B1-07's "done when" proves both paths agree once a run is done).
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


class RunCost(APIModel):
    """B2-06: matches `runs.cost`'s JSONB shape / UsageTracker.as_dict()
    exactly (app/usage.py)."""

    llm_input_tokens: int
    llm_output_tokens: int
    judge_calls: int
    stt_seconds: float
    tts_chars: int
    est_usd: float


EndReason = Literal["completed", "caller_ended", "agent_ended", "timeout", "cancelled", "error"]
# B2.6-03: how a run was started -- read-only here (populated at insert
# time, app/api/runs.py's _create_run / app/api/suites.py's fan-out
# insert), not settable via any request body.
Trigger = Literal["manual", "schedule", "ci", "api"]


class DashboardRunRow(APIModel):
    id: str
    suite: str
    agent: str
    status: Verdict
    pass_rate: str
    duration: str
    run_id: str
    # B2.5-01: NOT NULL on the runs table -- always present now.
    project_id: str
    end_reason: EndReason | None = None
    cost: RunCost | None = None
    # B2.5-03: a Test Run has parent_run_id IS NULL; a Call has it set.
    # `is_parent` is literally that predicate (matches `?isParent=true`'s
    # filter) -- it does NOT mean "has children"; a single simulation run
    # is a Test Run (is_parent=true) with zero DB-level children and one
    # *implicit* call, per this ticket's "do not special-case them" rule.
    is_parent: bool
    parent_run_id: str | None = None
    trigger: Trigger


class RunAggregate(APIModel):
    """B2.5-03: read-computed, never persisted -- reduced fresh from
    children (or, for a childless Test Run, from the run's own single
    implicit call) on every read, so it can never drift from what
    GET /v1/runs?parentRunId= would show for the same rows. B2.5-04 adds
    the *status* rollup (a separate, persisted, transactional concern);
    this block stays read-computed even after that lands.
    """

    call_count: int
    status_counts: dict[str, int]
    # None when no call has reached a scored terminal state yet (all idle).
    pass_rate: float | None
    latency_p50_ms: float | None
    latency_p90_ms: float | None
    latency_p95_ms: float | None
    total_cost_usd: float | None
    distinct_scenario_count: int
    distinct_persona_count: int


class RunDeltaRow(APIModel):
    """B2.5-05: `?fields=light`'s row shape -- only what the call queue
    repaints on each poll. Deliberately excludes everything RunDetail has
    that a queue row doesn't need (transcript, cost breakdown, ...) --
    that's the entire cost saving this endpoint exists for."""

    id: str
    status: str
    score: float | None
    turns: int | None
    duration_ms: int | None
    latency_p95: float | None
    # Null until B2.7-09 wires goal-vs-assertions into the judge -- present
    # in the shape now per stitch protocol §2 ("a tag means shapes are
    # stable, not endpoints work").
    goal_met: bool | None


class RunsDelta(APIModel):
    calls: list[RunDeltaRow]
    aggregate: RunAggregate


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
    # B2.5-01: NOT NULL on the runs table -- always present now.
    project_id: str
    end_reason: EndReason | None = None
    cost: RunCost | None = None
    # gs://... URI, playable via a signed URL generated on read (see
    # app/api/runs.py's get_run) -- null unless RECORDING_GCS_BUCKET is
    # configured server-side (app.engine.caller.recording).
    recording_url: str | None = None
    created_at: str
    transcript: list[TranscriptTurn]
    result_assertions: list[ResultAssertion]
    latency_series: list[float]
    # B2.5-03: see DashboardRunRow's comment -- same predicate, same
    # "do not special-case a single implicit call" rule.
    is_parent: bool
    parent_run_id: str | None = None
    trigger: Trigger
    # Present iff is_parent -- a Call's own detail has no aggregate to
    # show (it has no children of its own).
    aggregate: RunAggregate | None = None


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
