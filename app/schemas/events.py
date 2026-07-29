"""Event payload models — the `data` half of every run_events row.

Payloads are camelCase on the wire (matching the frontend's generated
TypeScript types per spine §2/§4's own examples: `latencyMs`, `assertionId`,
`triggeredAtTurn`); Python code uses snake_case attributes throughout.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class EventData(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class StatusData(EventData):
    status: Literal["queued", "claimed", "running", "completed", "cancelled", "failed"]


class TurnData(EventData):
    index: int
    role: Literal["caller", "agent"]
    text: str
    latency_ms: int | None = None
    flagged: bool = False
    flag_reason: str | None = None


class MetricsData(EventData):
    score: float | None = None
    avg_latency_ms: float | None = None
    turns_completed: int | None = None
    interruptions: int | None = None


class AssertionData(EventData):
    assertion_id: str
    name: str
    status: Literal["passed", "failed"]
    triggered_at_turn: int | None = None


class NodeData(EventData):
    node_id: str
    label: str
    x: float
    y: float
    state: str
    blocked_reason: str | None = None


class IntentData(EventData):
    name: str
    state: str
    path: str
    reason: str | None = None


class AttackData(EventData):
    category: str
    attack_prompt: str
    agent_response: str
    verdict: Literal["blocked", "leaked", "bypassed"] | None = None
    severity: str | None = None


class ExposureData(EventData):
    # Current tally per attack category — a snapshot, not a delta, so the
    # sidebar can always just render the latest frame (same "no two data
    # shapes" discipline as Amendment A).
    counts: dict[str, int]


class DoneData(EventData):
    """Type-specific completion payload (spine §4): populate whichever
    subset applies to the run's type (simulation | discovery | redteam)."""

    score: float | None = None
    result_badge: Literal["pass", "warn", "fail"] | None = None
    discovery_result: dict[str, object] | None = None
    findings_count: int | None = None
    exposure: dict[str, int] | None = None


class ErrorData(EventData):
    code: str
    message: str
    fatal: bool = False
