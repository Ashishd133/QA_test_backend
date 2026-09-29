"""Loads evals/cases/*.json into typed `EvalCase`s for the judge_evals
harness (tests/test_judge_evals.py). Kept separate from the test module so a
future consumer (e.g. a CLI to re-run a single case) can import it without
pulling in pytest.

B2.7-13: extended for goal-vs-assertion separation and metric compilation --
a case can now optionally carry a `goal` (+ hand-labeled `expected_goal_met`)
and `metric_ids` (+ hand-labeled `expected_metrics`), resolved against the
same `llm_judge`-kind builtins production scoring uses
(app.engine.metrics.builtins), never a second copy of their specs. Only
`llm_judge`-kind builtins are meaningful here: `builtin`-kind metrics
(response_latency, talk_ratio) are code-evaluated, not judge output, and
transcription_accuracy_wer is `not_computable` -- none of the three involve
judge accuracy, so a golden eval of judge output has nothing to say about
them (see app/engine/metrics/builtins.py's own module docstring).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from app.engine.judge.models import AssertionSpec, CompiledMetricSignal, TranscriptTurn
from app.engine.metrics.builtins import BUILTIN_METRICS
from evals.assertions import ASSERTIONS_BY_ID

_CASES_DIR = Path(__file__).parent / "cases"

# Only llm_judge-kind builtins are meaningful to a judge-accuracy eval --
# see this module's docstring.
LLM_JUDGE_METRICS_BY_KEY: dict[str, CompiledMetricSignal] = {
    m.key: CompiledMetricSignal(
        metric_id=m.key,
        name=m.name,
        description=str(m.spec.get("description", "")),
        distinguish_from=str(m.spec.get("distinguish_from", "")),
    )
    for m in BUILTIN_METRICS
    if m.kind == "llm_judge"
}


@dataclass(frozen=True)
class EvalCase:
    id: str
    description: str
    assertions: list[AssertionSpec]
    transcript: list[TranscriptTurn]
    expected: dict[str, Literal["passed", "failed"]]
    goal: str | None = None
    expected_goal_met: bool | None = None
    metrics: list[CompiledMetricSignal] = field(default_factory=list)
    expected_metrics: dict[str, Literal["passed", "failed", "warn"]] = field(default_factory=dict)


def load_cases() -> list[EvalCase]:
    cases = []
    for path in sorted(_CASES_DIR.glob("*.json")):
        raw = json.loads(path.read_text())
        metric_ids = raw.get("metric_ids", [])
        cases.append(
            EvalCase(
                id=raw["id"],
                description=raw["description"],
                assertions=[ASSERTIONS_BY_ID[aid] for aid in raw["assertion_ids"]],
                transcript=[TranscriptTurn(**turn) for turn in raw["transcript"]],
                expected=raw["expected"],
                goal=raw.get("goal"),
                expected_goal_met=raw.get("expected_goal_met"),
                metrics=[LLM_JUDGE_METRICS_BY_KEY[mid] for mid in metric_ids],
                expected_metrics=raw.get("expected_metrics", {}),
            )
        )
    if not cases:
        raise RuntimeError(f"no eval cases found in {_CASES_DIR}")
    return cases
