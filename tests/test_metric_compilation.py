"""B2.7-06 tests: metric compilation into the judge. No DB needed (pure
unit tests + a fake judge client, same pattern as tests/test_judge.py), so
these run in CI too. Judge *accuracy* with real metrics is a separate,
real-Gemini-backed concern -- see tests/test_judge_evals.py (judge_evals
marker), which this ticket's own done-when requires running afterward.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy.engine import RowMapping

from app.engine.judge.judge import (
    FinalJudge,
    GenAIClient,
    _Aio,
    _AioModels,
    _GenerateContentResponse,
)
from app.engine.judge.judge import _UsageMetadata as _JudgeUsageMetadata
from app.engine.judge.models import AssertionSpec, CompiledMetricSignal, FinalVerdict, MetricVerdict
from app.engine.judge.prompts import render_final_prompt
from app.engine.metrics.compiler import (
    compile_metrics,
    evaluate_builtin_metric,
    not_sampled_outcomes,
    outcomes_from_judge_verdicts,
    sampled_in,
)
from app.schemas.runs import TranscriptTurn
from app.usage import UsageTracker

_ASSERTIONS = [AssertionSpec(id="a1", name="Does X", description="Does X happen?")]
_TRANSCRIPT = [
    TranscriptTurn(role="caller", text="Hi, I need help."),
    TranscriptTurn(role="agent", text="Sure, let me help with that."),
]


def _row(**kwargs: object) -> RowMapping:
    # A plain dict satisfies RowMapping's __getitem__/.get duck typing --
    # same pattern tests/test_persona_spec.py already uses for a fake
    # RowMapping, just without the mypy cast noise here since compiler.py
    # only ever indexes/`.get`s these, never relies on RowMapping-specific
    # methods.
    return kwargs  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Template/prompt: byte-identical rendering when no metrics are given.
# ---------------------------------------------------------------------------


def test_no_metrics_renders_byte_identical_to_pre_change_output() -> None:
    with_no_arg = render_final_prompt(_ASSERTIONS, _TRANSCRIPT)
    with_empty_list = render_final_prompt(_ASSERTIONS, _TRANSCRIPT, metrics=[])
    assert with_no_arg == with_empty_list
    assert "## Metrics" not in with_no_arg


def test_metrics_render_as_additional_signal_blocks() -> None:
    signals = [
        CompiledMetricSignal(
            metric_id="m1",
            name="hallucination",
            description="No unsupported factual claims.",
            distinguish_from="Not about comprehension errors.",
        )
    ]
    prompt = render_final_prompt(_ASSERTIONS, _TRANSCRIPT, metrics=signals)
    assert "## Metrics" in prompt
    assert "m1: hallucination" in prompt
    assert "No unsupported factual claims." in prompt
    assert "Not about comprehension errors." in prompt


# ---------------------------------------------------------------------------
# FinalJudge.evaluate with metrics, via a fake client (same pattern as
# tests/test_judge.py -- no real network call).
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


async def test_final_judge_evaluate_threads_metrics_into_the_same_call() -> None:
    verdict = FinalVerdict(
        final_score=80,
        assertions=[],
        metrics=[
            MetricVerdict(
                metric_id="m1",
                analysis="the agent never made an unsupported claim",
                status="passed",
                value="true",
                turn_refs=[0],
                rationale="clean",
            )
        ],
        sentiment="neutral",
        summary="fine",
    )
    client = _FakeClient(
        aio=_FakeAio(models=_FakeModels(responses=[_FakeResponse(parsed=verdict, text="{}")]))
    )
    judge = FinalJudge(client, usage=UsageTracker())
    signals = [CompiledMetricSignal(metric_id="m1", name="hallucination", description="...")]

    result = await judge.evaluate(_ASSERTIONS, _TRANSCRIPT, metrics=signals)

    assert len(client.aio.models.calls) == 1  # one call, metrics batched in -- not a second call
    assert "## Metrics" in client.aio.models.calls[0]
    assert result.metrics[0].metric_id == "m1"
    assert result.metrics[0].value == "true"


async def test_final_judge_evaluate_without_metrics_is_unaffected() -> None:
    verdict = FinalVerdict(
        final_score=80, assertions=[], sentiment="neutral", summary="fine"
    )  # metrics omitted entirely -- defaults to []
    client = _FakeClient(
        aio=_FakeAio(models=_FakeModels(responses=[_FakeResponse(parsed=verdict, text="{}")]))
    )
    judge = FinalJudge(client, usage=UsageTracker())

    result = await judge.evaluate(_ASSERTIONS, _TRANSCRIPT)

    assert result.metrics == []
    assert "## Metrics" not in client.aio.models.calls[0]


# ---------------------------------------------------------------------------
# app.engine.metrics.compiler
# ---------------------------------------------------------------------------


def test_sampled_in_is_deterministic_for_the_same_run_and_metric() -> None:
    run_id = uuid.uuid4()
    metric_id = uuid.uuid4()
    first = sampled_in(run_id, metric_id, 50)
    second = sampled_in(run_id, metric_id, 50)
    assert first == second


def test_sampled_in_boundaries() -> None:
    run_id, metric_id = uuid.uuid4(), uuid.uuid4()
    assert sampled_in(run_id, metric_id, 100) is True
    assert sampled_in(run_id, metric_id, 0) is False


def test_compile_metrics_splits_by_kind_and_tracks_not_sampled() -> None:
    run_id = uuid.uuid4()
    always_in = _row(
        id=uuid.uuid4(),
        name="always_in_llm",
        kind="llm_judge",
        output_type="boolean",
        spec={"description": "d", "distinguish_from": "x"},
        sampling_pct=100,
        version=1,
    )
    always_out = _row(
        id=uuid.uuid4(),
        name="always_out_builtin",
        kind="builtin",
        output_type="numeric",
        spec={},
        sampling_pct=0,
        version=1,
    )
    python_row = _row(
        id=uuid.uuid4(),
        name="a_python_metric",
        kind="python",
        output_type="boolean",
        spec={"code": 'result = {"status": "passed", "value": None}'},
        sampling_pct=100,
        version=1,
    )
    compiled = compile_metrics([always_in, always_out, python_row], run_id=run_id)

    assert len(compiled.llm_judge_signals) == 1
    assert compiled.llm_judge_signals[0].name == "always_in_llm"
    assert len(compiled.llm_judge_rows) == 1
    assert len(compiled.python_rows) == 1
    assert len(compiled.builtin_rows) == 0
    assert len(compiled.not_sampled_rows) == 1
    assert compiled.not_sampled_rows[0]["name"] == "always_out_builtin"


def test_not_sampled_outcomes_shape() -> None:
    row = _row(id=uuid.uuid4(), name="x", version=3)
    outcomes = not_sampled_outcomes([row])
    assert len(outcomes) == 1
    assert outcomes[0].status == "not_sampled"
    assert outcomes[0].metric_version == 3
    assert outcomes[0].value is None


def test_outcomes_from_judge_verdicts_coerces_value_by_output_type() -> None:
    numeric_row = _row(id=uuid.uuid4(), name="n", version=1, output_type="numeric")
    boolean_row = _row(id=uuid.uuid4(), name="b", version=1, output_type="boolean")
    enum_row = _row(id=uuid.uuid4(), name="e", version=1, output_type="enum")
    rows = [numeric_row, boolean_row, enum_row]
    verdicts = [
        MetricVerdict(
            metric_id=str(numeric_row["id"]),
            analysis="a",
            status="passed",
            value="82.5",
            turn_refs=[0],
            rationale="r",
        ),
        MetricVerdict(
            metric_id=str(boolean_row["id"]),
            analysis="a",
            status="passed",
            value="true",
            turn_refs=[0],
            rationale="r",
        ),
        MetricVerdict(
            metric_id=str(enum_row["id"]),
            analysis="a",
            status="warn",
            value="Review",
            turn_refs=[0],
            rationale="r",
        ),
    ]
    outcomes = {o.metric_id: o for o in outcomes_from_judge_verdicts(verdicts, rows)}
    assert outcomes[str(numeric_row["id"])].value == 82.5
    assert outcomes[str(boolean_row["id"])].value is True
    assert outcomes[str(enum_row["id"])].value == "Review"
    assert outcomes[str(enum_row["id"])].status == "warn"


def test_outcomes_from_judge_verdicts_errors_on_a_missing_verdict() -> None:
    """B2.7-04's "never a silent pass" bar: a metric sent to the judge
    that never got a verdict back becomes an explicit error, not a
    silently-dropped row."""
    row = _row(id=uuid.uuid4(), name="x", version=1, output_type="boolean")
    outcomes = outcomes_from_judge_verdicts([], [row])
    assert len(outcomes) == 1
    assert outcomes[0].status == "error"
    assert outcomes[0].value is None


def test_evaluate_builtin_response_latency() -> None:
    row = _row(id=uuid.uuid4(), name="response_latency", version=1, spec={"threshold": 3000})
    fast = evaluate_builtin_metric(row, [], [100.0, 200.0])
    assert fast.status == "passed"
    assert fast.value == 150.0
    slow = evaluate_builtin_metric(row, [], [5000.0])
    assert slow.status == "failed"


def test_evaluate_builtin_response_latency_not_computable_with_no_data() -> None:
    row = _row(id=uuid.uuid4(), name="response_latency", version=1, spec={})
    outcome = evaluate_builtin_metric(row, [], [])
    assert outcome.status == "not_computable"


def test_evaluate_builtin_talk_ratio() -> None:
    row = _row(id=uuid.uuid4(), name="talk_ratio", version=1, spec={"threshold": 0.6})
    turns = [
        TranscriptTurn(role="agent", text="one two three four"),
        TranscriptTurn(role="caller", text="one two"),
    ]
    outcome = evaluate_builtin_metric(row, turns, [])
    assert outcome.value == round(4 / 6, 3)
    assert outcome.status == "failed"  # 0.667 > 0.6 threshold


def test_evaluate_builtin_wer_is_honestly_not_computable() -> None:
    row = _row(id=uuid.uuid4(), name="transcription_accuracy_wer", version=1, spec={})
    outcome = evaluate_builtin_metric(row, [], [])
    assert outcome.status == "not_computable"
    assert outcome.value is None


def test_evaluate_builtin_unrecognized_key_is_error_not_a_crash() -> None:
    row = _row(id=uuid.uuid4(), name="not_a_real_builtin", version=1, spec={})
    outcome = evaluate_builtin_metric(row, [], [])
    assert outcome.status == "error"
