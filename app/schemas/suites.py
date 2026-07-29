"""Request/response models for /v1/suites and /v1/scenarios (B1-01).

Field names match the frontend's `src/types/index.ts` Suite/Scenario
interfaces verbatim (provided directly, since CADENCE_API_ARCHITECTURE.md
wasn't available). camelCase on the wire via alias_generator=to_camel, same
convention as event payloads (app/schemas/events.py).

`status: Verdict` is a computed judgment (pass/warn/fail/idle) derived from
a scenario's most recent run, not the DB's raw execution-lifecycle
runs.status enum ('queued'|'claimed'|...); see app/api/suites.py.
"""

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from app.verdict import Verdict


class APIModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ScenarioSummary(APIModel):
    id: str
    suite_id: str
    name: str
    persona: str
    assert_count: int
    status: Verdict
    score: str
    run_id: str


class SuiteListItem(APIModel):
    id: str
    name: str
    desc: str
    agent: str
    last_run: str
    pass_rate: str
    pr: int
    count: int


class SuiteDetail(SuiteListItem):
    scenarios: list[ScenarioSummary]


class SuiteCreate(APIModel):
    name: str
    description: str | None = None
    agent_id: str


class SuiteUpdate(APIModel):
    name: str | None = None
    description: str | None = None
    agent_id: str | None = None


class ScenarioCreateRequest(APIModel):
    """Manual creation requires name+persona; `fromDraftId` short-circuits
    that (B1-01: idempotent via scenarios.source_draft_ref UNIQUE)."""

    from_draft_id: str | None = None
    name: str | None = None
    persona: str | None = None
    persona_initials: str | None = None
    script: dict[str, object] | None = None
    assertions: list[object] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_shape(self) -> "ScenarioCreateRequest":
        if self.from_draft_id is None and (not self.name or not self.persona):
            raise ValueError("name and persona are required when fromDraftId is not provided")
        return self


class ScenarioUpdate(APIModel):
    name: str | None = None
    persona: str | None = None
    script: dict[str, object] | None = None
    assertions: list[object] | None = None
