"""B2-05: judge unit tests -- malformed-JSON retry path + prompt rendering.

These don't call a real model (no network, no cost, deterministic) -- they
exercise `_generate_verdict`'s retry logic directly via a fake client that
returns controlled sequences of responses, and check the Jinja2 templates
render the house-style structure (signal definitions, distinguish-from,
transcript with citable indices) without error. Judge *accuracy* against
real transcripts is validated separately (a real model is non-deterministic
and costs money/latency; see `evals/` once B2-06 builds the golden harness).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from pydantic import BaseModel

from app.engine.judge.judge import (
    FinalJudge,
    GenAIClient,
    IncrementalJudge,
    JudgeOutputError,
    _Aio,
    _AioModels,
    _generate_verdict,
    _GenerateContentResponse,
    _UsageMetadata,
)
from app.engine.judge.models import AssertionSpec, FinalVerdict, IncrementalVerdict, TranscriptTurn
from app.engine.judge.prompts import render_final_prompt, render_incremental_prompt
from app.usage import UsageTracker


class _DummyVerdict(BaseModel):
    ok: bool


@dataclass
class _FakeUsageMetadata(_UsageMetadata):
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


def _client_returning(*responses: _FakeResponse) -> _FakeClient:
    return _FakeClient(aio=_FakeAio(models=_FakeModels(responses=list(responses))))


async def test_valid_response_on_first_attempt_needs_no_retry() -> None:
    client = _client_returning(_FakeResponse(parsed=_DummyVerdict(ok=True), text='{"ok": true}'))

    result = await _generate_verdict(
        client,
        model="fake-model",
        prompt="prompt",
        response_model=_DummyVerdict,
        prompt_version="v1",
        span_name="test.span",
        usage=UsageTracker(),
    )

    assert result == _DummyVerdict(ok=True)
    assert len(client.aio.models.calls) == 1


async def test_malformed_json_is_retried_and_recovers() -> None:
    client = _client_returning(
        _FakeResponse(parsed=None, text="not valid json"),
        _FakeResponse(parsed=_DummyVerdict(ok=True), text='{"ok": true}'),
    )

    result = await _generate_verdict(
        client,
        model="fake-model",
        prompt="prompt",
        response_model=_DummyVerdict,
        prompt_version="v1",
        span_name="test.span",
        usage=UsageTracker(),
    )

    assert result == _DummyVerdict(ok=True)
    assert len(client.aio.models.calls) == 2
    # The retry prompt must still carry the original instructions forward,
    # not just complain about the previous failure in isolation.
    assert "prompt" in client.aio.models.calls[1]
    assert "not valid JSON" in client.aio.models.calls[1]


async def test_malformed_json_past_retry_budget_raises() -> None:
    client = _client_returning(
        _FakeResponse(parsed=None, text="still not json"),
        _FakeResponse(parsed=None, text="still not json"),
    )

    with pytest.raises(JudgeOutputError):
        await _generate_verdict(
            client,
            model="fake-model",
            prompt="prompt",
            response_model=_DummyVerdict,
            prompt_version="v1",
            span_name="test.span",
            usage=UsageTracker(),
        )

    assert len(client.aio.models.calls) == 2  # initial attempt + exactly one retry


async def test_wrong_type_parsed_is_treated_as_malformed() -> None:
    """`response.parsed` can be a non-None object of the wrong type (the SDK
    parses against whatever schema it was given) -- must not be mistaken for
    a valid result."""
    client = _client_returning(
        _FakeResponse(parsed={"unexpected": "shape"}, text="{}"),
        _FakeResponse(parsed=_DummyVerdict(ok=False), text='{"ok": false}'),
    )

    result = await _generate_verdict(
        client,
        model="fake-model",
        prompt="prompt",
        response_model=_DummyVerdict,
        prompt_version="v1",
        span_name="test.span",
        usage=UsageTracker(),
    )

    assert result == _DummyVerdict(ok=False)
    assert len(client.aio.models.calls) == 2


async def test_usage_tracker_records_tokens_and_calls_per_attempt() -> None:
    """A retry is a real, separate API call -- both attempts' token usage
    must accumulate, not just the one that finally succeeds."""
    client = _client_returning(
        _FakeResponse(
            parsed=None,
            text="not valid json",
            usage_metadata=_FakeUsageMetadata(prompt_token_count=100, candidates_token_count=10),
        ),
        _FakeResponse(
            parsed=_DummyVerdict(ok=True),
            text='{"ok": true}',
            usage_metadata=_FakeUsageMetadata(prompt_token_count=120, candidates_token_count=8),
        ),
    )
    usage = UsageTracker()

    await _generate_verdict(
        client,
        model="fake-model",
        prompt="prompt",
        response_model=_DummyVerdict,
        prompt_version="v1",
        span_name="test.span",
        usage=usage,
    )

    assert usage.judge_calls == 2
    assert usage.llm_input_tokens == 220
    assert usage.llm_output_tokens == 18


async def test_usage_tracker_shared_across_incremental_and_final_judge() -> None:
    """The same tracker instance passed to both judges accumulates one
    run's total across both passes, per IncrementalJudge/FinalJudge's
    documented sharing convention."""
    incremental_client = _client_returning(
        _FakeResponse(
            parsed=IncrementalVerdict(flips=[], live_score=50),
            text="{}",
            usage_metadata=_FakeUsageMetadata(prompt_token_count=200, candidates_token_count=20),
        )
    )
    final_client = _client_returning(
        _FakeResponse(
            parsed=FinalVerdict(
                final_score=80,
                assertions=[],
                sentiment="neutral",
                summary="ok",
            ),
            text="{}",
            usage_metadata=_FakeUsageMetadata(prompt_token_count=300, candidates_token_count=30),
        )
    )
    usage = UsageTracker()
    incremental_judge = IncrementalJudge(incremental_client, usage=usage)
    final_judge = FinalJudge(final_client, usage=usage)

    await incremental_judge.evaluate(_ASSERTIONS, _TRANSCRIPT, statuses={})
    await final_judge.evaluate(_ASSERTIONS, _TRANSCRIPT)

    assert usage is incremental_judge.usage is final_judge.usage
    assert usage.judge_calls == 2
    assert usage.llm_input_tokens == 500
    assert usage.llm_output_tokens == 50
    assert usage.est_usd > 0


_ASSERTIONS = [
    AssertionSpec(
        id="a1",
        name="Requests identity verification before action",
        description="Agent asks for identifying details before acting on the account.",
        distinguish_from="Not satisfied merely by a greeting -- must ask for specific details.",
    ),
]

_TRANSCRIPT = [
    TranscriptTurn(role="caller", text="Hi, I need to block my card."),
    TranscriptTurn(role="agent", text="Can you verify your name and date of birth?"),
]


def test_incremental_prompt_includes_house_style_sections() -> None:
    prompt = render_incremental_prompt(_ASSERTIONS, _TRANSCRIPT, statuses={})

    assert "Requests identity verification before action" in prompt
    assert "Distinguish from:" in prompt
    assert "Not satisfied merely by a greeting" in prompt
    assert "[0] caller: Hi, I need to block my card." in prompt
    assert "[1] agent: Can you verify your name and date of birth?" in prompt
    assert "undetermined" in prompt  # default status when none supplied


def test_incremental_prompt_reflects_current_status() -> None:
    prompt = render_incremental_prompt(_ASSERTIONS, _TRANSCRIPT, statuses={"a1": "passed"})
    assert "Current status: passed" in prompt


def test_final_prompt_has_no_undetermined_escape_hatch() -> None:
    prompt = render_final_prompt(_ASSERTIONS, _TRANSCRIPT)

    assert "Requests identity verification before action" in prompt
    assert "there is no" in prompt.lower()  # instructs against leaving signals unresolved
    assert "[0] caller: Hi, I need to block my card." in prompt
