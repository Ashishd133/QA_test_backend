"""Request/response models for /v1/suites and /v1/scenarios (B1-01).

Field names match the frontend's `src/types/index.ts` Suite/Scenario
interfaces verbatim (provided directly, since CADENCE_API_ARCHITECTURE.md
wasn't available). camelCase on the wire via alias_generator=to_camel, same
convention as event payloads (app/schemas/events.py).

`status: Verdict` is a computed judgment (pass/warn/fail/idle) derived from
a scenario's most recent run, not the DB's raw execution-lifecycle
runs.status enum ('queued'|'claimed'|...); see app/api/suites.py.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from app.verdict import Verdict


class APIModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ScenarioMetricAttachment(APIModel):
    """B2.7-11: which metrics a scenario attaches, and whether each gates
    the call's verdict (B2.7-08 reads exactly this via `scenario_metrics`).
    Attaching a metric here does NOT change which metrics get *scored* for
    the call -- that's resolve_metrics_for_agent's job (agent > project >
    builtin), unaffected by this table. This is purely the gating flag."""

    metric_id: str
    gating: bool = False


class ScenarioSummary(APIModel):
    id: str
    suite_id: str
    name: str
    persona: str
    persona_id: str | None = None
    assert_count: int
    status: Verdict
    score: str
    run_id: str


class ScenarioDetail(ScenarioSummary):
    goal: str | None = None
    test_profile_id: str | None = None
    conditions: dict[str, object] | None = None
    script: dict[str, object] | None = None
    assertions: list[object] = Field(default_factory=list)
    metrics: list[ScenarioMetricAttachment] = Field(default_factory=list)


class SuiteListItem(APIModel):
    id: str
    project_id: str
    name: str
    desc: str
    agent: str
    last_run: str
    pass_rate: str
    pr: int
    count: int
    folder: str | None = None
    rubric: dict[str, object] | None = None


class SuiteDetail(SuiteListItem):
    scenarios: list[ScenarioSummary]


class SuiteCreate(APIModel):
    name: str
    description: str | None = None
    agent_id: str
    folder: str | None = None
    rubric: dict[str, object] | None = None


class SuiteUpdate(APIModel):
    name: str | None = None
    description: str | None = None
    agent_id: str | None = None
    folder: str | None = None
    rubric: dict[str, object] | None = None


class SuiteMoveRequest(APIModel):
    """Bulk move (B2.7-11): reassign `folder` on many suites in one call.
    `folder: None` moves them back to the top level, same meaning as the
    column's own NULL -- not the empty string."""

    suite_ids: list[str]
    folder: str | None = None


class ScenarioCreateRequest(APIModel):
    """Manual creation requires name+personaId; `fromDraftId` short-circuits
    that (B1-01: idempotent via scenarios.source_draft_ref UNIQUE). A
    from-draft creation may still pass `personaId` to override the draft's
    own (free-text, unresolved) persona suggestion; when omitted, the
    server tries an exact case-insensitive name match against personas
    visible to the project and 422s (`persona_not_found`) if none matches."""

    from_draft_id: str | None = None
    name: str | None = None
    persona_id: str | None = None
    script: dict[str, object] | None = None
    assertions: list[object] = Field(default_factory=list)
    goal: str | None = None
    test_profile_id: str | None = None
    conditions: dict[str, object] | None = None
    metrics: list[ScenarioMetricAttachment] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_shape(self) -> "ScenarioCreateRequest":
        if self.from_draft_id is None and (not self.name or not self.persona_id):
            raise ValueError("name and personaId are required when fromDraftId is not provided")
        return self


class ScenarioUpdate(APIModel):
    name: str | None = None
    persona_id: str | None = None
    script: dict[str, object] | None = None
    assertions: list[object] | None = None
    goal: str | None = None
    test_profile_id: str | None = None
    conditions: dict[str, object] | None = None
    metrics: list[ScenarioMetricAttachment] | None = None


class ScenarioDuplicateRequest(APIModel):
    """B2.7-11 bulk op: copy a scenario into another suite, optionally in
    another project. `targetProjectId` omitted/equal to the caller's own
    project means a same-project duplicate (persona/testProfile refs reused
    as-is); a genuinely cross-project duplicate copies any project-scoped
    (non-builtin) persona/test-profile by value into the target project
    rather than leaving a dangling cross-project reference."""

    target_suite_id: str
    target_project_id: str | None = None


class SuiteRunCreate(APIModel):
    """B2.6-01. `scenario_ids` omitted/None means "every scenario in the
    suite". `persona_ids`/`condition_profile_ids` are accepted on the wire
    now (the frontend backlog's W2.6-01 run-config modal already builds
    against them) but rejected with 422 `not_supported` if non-empty until
    B2.7 wires personas/condition profiles to scenarios -- shape stable,
    behavior honest, no silently-dropped input."""

    agent_id: str
    scenario_ids: list[str] | None = None
    persona_ids: list[str] | None = None
    condition_profile_ids: list[str] | None = None


class SuiteRunCreateResponse(APIModel):
    parent_run_id: str
    call_count: int


class ScenarioGenerateRequest(APIModel):
    """B2.7-12. `discoveryRunId` is required exactly when
    `source='discovery_run'` -- rejected otherwise so a caller can't pass
    one that silently gets ignored, or omit one that silently gets treated
    as "no context"."""

    source: Literal["agent_prompt", "agent_description", "discovery_run"]
    discovery_run_id: str | None = None
    count: int = 8

    @model_validator(mode="after")
    def _check_shape(self) -> "ScenarioGenerateRequest":
        if not (1 <= self.count <= 20):
            raise ValueError("count must be between 1 and 20")
        has_id = self.discovery_run_id is not None
        if (self.source == "discovery_run") != has_id:
            raise ValueError("discoveryRunId is required if and only if source is discovery_run")
        return self


class GeneratedDraftSummary(APIModel):
    draft_id: str
    name: str
    kind: Literal["positive", "negative"]
    persona: str
    persona_id: str | None = None
    goal: str
    assertions: list[object]
    metric_names: list[str] = Field(default_factory=list)


class ScenarioGenerateResponse(APIModel):
    drafts: list[GeneratedDraftSummary]
    cost: dict[str, object]
