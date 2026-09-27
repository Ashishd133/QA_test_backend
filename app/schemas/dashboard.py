"""Request/response models for GET /v1/metrics/dashboard (B1-06).
DashboardMetrics matches the frontend's src/types/index.ts verbatim.

Persona used to live here -- moved to app/schemas/personas.py by B2.7-02.
"""

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class APIModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class Outcome(APIModel):
    passed: int
    warnings: int
    failed: int
    pass_pct: int


class DashboardMetrics(APIModel):
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
