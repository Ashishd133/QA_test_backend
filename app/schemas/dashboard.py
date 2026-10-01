"""Request/response models for GET /v1/metrics/dashboard (B1-06, superseded
by B2.8-01/02/04) and GET /v1/metrics/failing-checks (B2.8-03).

DashboardMetrics keeps every v1 field verbatim (per B2.8-01's own ticket
text: "supersedes B1-06's v1 shape; keep v1 responding for one release") and
adds the v2 fields alongside them, rather than replacing the path or the
shape -- one endpoint, one response model, so `contract-v1.4.0`'s consumers
never have to choose which shape they're reading.

Persona used to live here -- moved to app/schemas/personas.py by B2.7-02.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.schemas.agents import AgentHealth
from app.schemas.runs import Trigger
from app.verdict import Verdict

Window = Literal["24h", "7d", "30d"]


class APIModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class Outcome(APIModel):
    passed: int
    warnings: int
    failed: int
    pass_pct: int


class PostureCards(APIModel):
    """B2.8-01. All four cards share one `window` (the request's own
    `window` query param) and compare against the immediately preceding
    window of the same length -- `pass_rate_delta_pts`/`latency_p95_delta_ms`
    are `current - prior`, not a percentage change."""

    pass_rate_pct: float
    pass_rate_delta_pts: float
    calls_run: int
    calls_failed: int
    calls_cancelled: int
    # None only when the window has zero turns with recorded latency.
    latency_p95_ms: float | None
    latency_p95_delta_ms: float | None
    latency_threshold_ms: int
    open_critical_findings: int
    # category -> open critical-finding count, e.g. {"pii_exfiltration": 2}
    open_critical_findings_by_category: dict[str, int]


class SuitePassRateSeries(APIModel):
    """Always the trailing 14 calendar days regardless of `window` -- the
    redesign's own "Daily, last 14 days" caption, matching v1's trend_bars
    convention rather than being re-scoped by the window selector."""

    suite_id: str
    suite_name: str
    # 14 entries, oldest -> newest. None = no completed calls that day
    # (a low-n gap, not a 0% day -- B2.8's own "done when" bar).
    daily_pass_rate: list[float | None]


class LatencyPercentiles(APIModel):
    """B2.8-02. Same `window` as the posture cards."""

    p50_ms: float | None
    p90_ms: float | None
    p95_ms: float | None
    threshold_ms: int
    total_turns: int
    flagged_turns: int
    # Fixed 12 buckets from 0 to a ~2x-p95-rounded ceiling; empty windows
    # return 12 zeros, not an empty list.
    histogram: list[int]


class SpendSummary(APIModel):
    """B2.8-04. `credits_remaining_pct` is null whenever the project has no
    `budget_usd_monthly` configured -- the UI hides the bar rather than
    showing a fake 0%, per the ticket's own "done when" bar."""

    cost_per_call: float | None
    window_spend: float
    credits_remaining_pct: float | None


class RecentRun(APIModel):
    id: str
    name: str
    agent_name: str
    trigger: Trigger
    call_count: int
    # None while the batch is still running (verdict_for_run's "idle").
    pass_rate: float | None
    status: Verdict


class DashboardMetrics(APIModel):
    # --- v1 fields, unchanged (B1-06 -> kept alive one release) ---
    pass_rate: str
    pass_rate_delta: str
    # to_camel mishandles the numeric suffix (produces "testRuns7D", not
    # "testRuns7d"), so pin the wire name explicitly for this one field.
    test_runs_7d: str = Field(alias="testRuns7d")
    test_runs_delta: str
    avg_latency: str
    avg_latency_delta: str
    scenario_coverage: str
    scenario_coverage_delta: str
    trend_bars: list[int]
    outcome: Outcome
    # --- v2 additions (B2.8-01/02/04) ---
    window: Window
    posture: PostureCards
    pass_rate_by_suite: list[SuitePassRateSeries]
    latency: LatencyPercentiles
    spend: SpendSummary
    recent_runs: list[RecentRun]
    agent_health: list[AgentHealth]


class FailingCheck(APIModel):
    """B2.8-03. `kind`/`check_id` together identify the check across the
    two source tables (`assertion_results`/`metric_results`) that don't
    share an id space. `drilldown_filter` is the exact `GET /v1/runs` query
    string a click-through appends -- server-defined so the dashboard's
    count and the calls list's count can't drift apart (the ticket's own
    "done when": clicking a row returns exactly `fail_count` calls)."""

    kind: Literal["assertion", "metric"]
    check_id: str
    name: str
    scope_label: str
    fail_count: int
    total_count: int
    rate_pct: float
    last_seen_at: str | None
    drilldown_filter: str
