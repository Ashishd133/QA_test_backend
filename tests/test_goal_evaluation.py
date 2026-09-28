"""B2.7-09 tests: goal vs assertions separated in the judge. No DB needed
(pure unit tests + a fake judge client, same pattern as tests/test_judge.py
and tests/test_metric_compilation.py), so these run in CI too.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.engine.judge.judge import (
    FinalJudge,
    GenAIClient,
    _Aio,
    _AioModels,
    _GenerateContentResponse,
)
from app.engine.judge.judge import _UsageMetadata as _JudgeUsageMetadata
from app.engine.judge.models import AssertionSpec, FinalAssertionNote, FinalVerdict
from app.engine.judge.prompts import render_final_prompt
from app.schemas.runs import TranscriptTurn
from app.usage import UsageTracker

_ASSERTIONS = [AssertionSpec(id="a1", name="Does X", description="Does X happen?")]
_TRANSCRIPT = [
    TranscriptTurn(role="caller", text="I need my card blocked."),
    TranscriptTurn(role="agent", text="Sure, let me verify your identity first."),
]


# ---------------------------------------------------------------------------
# Template/prompt: byte-identical rendering when no goal is given.
# ---------------------------------------------------------------------------


def test_no_goal_renders_byte_identical_to_pre_change_output() -> None:
    with_no_arg = render_final_prompt(_ASSERTIONS, _TRANSCRIPT)
    with_none = render_final_prompt(_ASSERTIONS, _TRANSCRIPT, goal=None)
    with_empty_string = render_final_prompt(_ASSERTIONS, _TRANSCRIPT, goal="")
    assert with_no_arg == with_none == with_empty_string
    assert "## Goal" not in with_no_arg


def test_goal_renders_as_its_own_section() -> None:
    prompt = render_final_prompt(_ASSERTIONS, _TRANSCRIPT, goal="Get the caller's card blocked.")
    assert "## Goal" in prompt
    assert "Get the caller's card blocked." in prompt
    assert "goal_analysis" in prompt
    assert "goal_met" in prompt


# ---------------------------------------------------------------------------
# FinalJudge.evaluate with a goal, via a fake client (no real network call).
# ---------------------------------------------------------------------------


@dataclass
class _FakeUsageMetadata(_JudgeUsageMetadata):
    prompt_token_count: int | None
    candidates_token_count: int | None


@dataclass
class _FakeResponse(_GenerateContentResponse):
    parsed: object
    text: str | None = None
    usage_metadata: _FakeUsageMetadata | None = None


@dataclass
class _FakeModels(_AioModels):
    responses: list[_FakeResponse]
    calls: list[str] = field(default_factory=list)

    async def generate_content(self, *, model: str, contents: str, config: object) -> _FakeResponse:
        self.calls.append(contents)
        return self.responses[len(self.calls) - 1]


@dataclass
class _FakeAio(_Aio):
    models: _FakeModels


@dataclass
class _FakeClient(GenAIClient):
    aio: _FakeAio


def _client_returning(verdict: FinalVerdict) -> _FakeClient:
    return _FakeClient(
        aio=_FakeAio(models=_FakeModels(responses=[_FakeResponse(parsed=verdict, text="{}")]))
    )


async def test_every_assertion_passes_but_goal_not_met() -> None:
    """The ticket's own literal done-when: 'passed the steps, failed the
    job' -- a coarse pass/fail on assertions alone would hide this."""
    verdict = FinalVerdict(
        final_score=40,
        assertions=[
            FinalAssertionNote(
                assertion_id="a1",
                analysis="the agent did ask for identity verification",
                status="passed",
                turn_refs=[1],
                note="verification requested",
            )
        ],
        goal_analysis=(
            "the caller asked to block their card; verification was requested but "
            "the transcript ends before the card was ever actually blocked"
        ),
        goal_met=False,
        goal_turn_refs=[1],
        sentiment="neutral",
        summary="verification started, card never blocked",
    )
    client = _client_returning(verdict)
    judge = FinalJudge(client, usage=UsageTracker())

    result = await judge.evaluate(_ASSERTIONS, _TRANSCRIPT, goal="Get the caller's card blocked.")

    assert all(a.status == "passed" for a in result.assertions)
    assert result.goal_met is False
    assert result.goal_turn_refs == [1]
    assert "## Goal" in client.aio.models.calls[0]


async def test_evaluate_without_goal_leaves_goal_fields_none() -> None:
    verdict = FinalVerdict(
        final_score=80, assertions=[], sentiment="neutral", summary="fine"
    )  # goal_met/goal_analysis omitted entirely -- defaults to None
    client = _client_returning(verdict)
    judge = FinalJudge(client, usage=UsageTracker())

    result = await judge.evaluate(_ASSERTIONS, _TRANSCRIPT)

    assert result.goal_met is None
    assert result.goal_analysis is None
    assert result.goal_turn_refs == []
    assert "## Goal" not in client.aio.models.calls[0]
