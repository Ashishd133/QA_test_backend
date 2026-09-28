"""B2.7-06: turns a scenario's resolved metric set (app.engine.metrics.
resolver) into judge-ready signals, builtin-code dispatch, and sandbox
dispatch -- compiled once per run, not once per metric, so cost doesn't
scale with metric count. `llm_judge` metrics are batched into the SAME
final-pass judge call that already scores assertions (see
app.engine.judge.judge.FinalJudge.evaluate's `metrics` kwarg); `builtin`
metrics are scored directly from already-recorded Turn/LatencyClock data;
`python` metrics run through the sandbox (app.engine.metrics.sandbox).

Design choice, stated once here: metrics are evaluated only at final-pass
time, not incrementally per turn -- same reasoning as B2.7-09's goal
(a holistic judgment fits the end-of-call pass better than a live flip),
and it's what keeps this ticket's own "≤2 LLM calls per turn" bar
trivially true: zero *new* per-turn calls are added at all.
"""

from __future__ import annotations

import hashlib
import statistics as _statistics
import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.engine import RowMapping

from app.engine.judge.models import CompiledMetricSignal, MetricVerdict
from app.engine.metrics.sandbox import run_python_metric
from app.schemas.runs import TranscriptTurn

MetricStatus = Literal["passed", "failed", "warn", "error", "not_sampled", "not_computable"]


@dataclass(frozen=True)
class MetricOutcome:
    metric_id: str
    metric_version: int
    status: MetricStatus
    value: object | None
    turn_refs: list[int]
    rationale: str | None


@dataclass(frozen=True)
class CompiledMetrics:
    llm_judge_signals: list[CompiledMetricSignal]
    llm_judge_rows: list[RowMapping]
    builtin_rows: list[RowMapping]
    python_rows: list[RowMapping]
    not_sampled_rows: list[RowMapping]


def sampled_in(run_id: uuid.UUID, metric_id: object, sampling_pct: int) -> bool:
    """Deterministic, not random-per-call: replaying the same `run_id`
    (scripts/replay_run.py) reproduces the same sampling decision instead
    of a metric flip-flopping between not_sampled and scored on rerun."""
    if sampling_pct >= 100:
        return True
    if sampling_pct <= 0:
        return False
    digest = hashlib.sha256(f"{run_id}:{metric_id}".encode()).hexdigest()
    return int(digest, 16) % 100 < sampling_pct


def compile_metrics(resolved: list[RowMapping], *, run_id: uuid.UUID) -> CompiledMetrics:
    llm_judge = [r for r in resolved if r["kind"] == "llm_judge"]
    builtin = [r for r in resolved if r["kind"] == "builtin"]
    python = [r for r in resolved if r["kind"] == "python"]

    sampled_llm_judge = [r for r in llm_judge if sampled_in(run_id, r["id"], r["sampling_pct"])]
    sampled_builtin = [r for r in builtin if sampled_in(run_id, r["id"], r["sampling_pct"])]
    sampled_python = [r for r in python if sampled_in(run_id, r["id"], r["sampling_pct"])]

    sampled_ids = {r["id"] for r in (*sampled_llm_judge, *sampled_builtin, *sampled_python)}
    not_sampled = [r for r in resolved if r["id"] not in sampled_ids]

    signals = [
        CompiledMetricSignal(
            metric_id=str(row["id"]),
            name=row["name"],
            description=str((row["spec"] or {}).get("description", "")),
            distinguish_from=str((row["spec"] or {}).get("distinguish_from", "")),
        )
        for row in sampled_llm_judge
    ]
    return CompiledMetrics(
        llm_judge_signals=signals,
        llm_judge_rows=sampled_llm_judge,
        builtin_rows=sampled_builtin,
        python_rows=sampled_python,
        not_sampled_rows=not_sampled,
    )


def not_sampled_outcomes(rows: list[RowMapping]) -> list[MetricOutcome]:
    return [
        MetricOutcome(
            metric_id=str(row["id"]),
            metric_version=row["version"],
            status="not_sampled",
            value=None,
            turn_refs=[],
            rationale=None,
        )
        for row in rows
    ]


def _coerce_value(raw: str, output_type: str) -> object:
    if output_type == "numeric":
        try:
            return float(raw)
        except ValueError:
            return raw  # keep the judge's raw text rather than crash on a malformed number
    if output_type == "boolean":
        return raw.strip().lower() in ("true", "yes", "1")
    return raw  # enum / tri_state: the judge's own label text, unchanged


def outcomes_from_judge_verdicts(
    verdicts: list[MetricVerdict], rows: list[RowMapping]
) -> list[MetricOutcome]:
    """Every row that was actually sent to the judge gets exactly one
    outcome back: a real verdict if the judge returned one, or an "error"
    outcome (not a silent drop) if the judge's response omitted it --
    B2.7-04's own "never a crashed executor, never a silent pass" bar
    applies here too, not just to the sandbox."""
    rows_by_id = {str(r["id"]): r for r in rows}
    verdicts_by_id = {v.metric_id: v for v in verdicts}
    outcomes = []
    for metric_id, row in rows_by_id.items():
        verdict = verdicts_by_id.get(metric_id)
        if verdict is None:
            outcomes.append(
                MetricOutcome(
                    metric_id=metric_id,
                    metric_version=row["version"],
                    status="error",
                    value=None,
                    turn_refs=[],
                    rationale="judge response did not include a verdict for this metric",
                )
            )
            continue
        outcomes.append(
            MetricOutcome(
                metric_id=metric_id,
                metric_version=row["version"],
                status=verdict.status,
                value=_coerce_value(verdict.value, row["output_type"]),
                turn_refs=verdict.turn_refs,
                rationale=verdict.rationale,
            )
        )
    return outcomes


def evaluate_builtin_metric(
    row: RowMapping, turns: list[TranscriptTurn], latencies_ms: list[float]
) -> MetricOutcome:
    key = row["name"]
    spec = row["spec"] or {}
    threshold = spec.get("threshold")
    metric_id, version = str(row["id"]), row["version"]

    if key == "response_latency":
        if not latencies_ms:
            return MetricOutcome(
                metric_id, version, "not_computable", None, [], "no agent turns with latency data"
            )
        mean_ms = _statistics.mean(latencies_ms)
        status: MetricStatus = "passed" if threshold is None or mean_ms <= threshold else "failed"
        return MetricOutcome(metric_id, version, status, round(mean_ms, 1), [], None)

    if key == "talk_ratio":
        agent_words = sum(len(t.text.split()) for t in turns if t.role == "agent")
        caller_words = sum(len(t.text.split()) for t in turns if t.role == "caller")
        total = agent_words + caller_words
        if total == 0:
            return MetricOutcome(
                metric_id, version, "not_computable", None, [], "no words in transcript"
            )
        ratio = agent_words / total
        status = "passed" if threshold is None or ratio <= threshold else "failed"
        return MetricOutcome(metric_id, version, status, round(ratio, 3), [], None)

    if key == "transcription_accuracy_wer":
        return MetricOutcome(
            metric_id,
            version,
            "not_computable",
            None,
            [],
            "no reference transcript recorded (see app.engine.metrics.builtins)",
        )

    return MetricOutcome(
        metric_id, version, "error", None, [], f"unrecognized builtin metric key {key!r}"
    )


async def evaluate_python_metric(row: RowMapping, context: dict[str, object]) -> MetricOutcome:
    spec = row["spec"] or {}
    code = spec.get("code")
    metric_id, version = str(row["id"]), row["version"]
    if not isinstance(code, str) or not code.strip():
        return MetricOutcome(metric_id, version, "error", None, [], "metric has no spec.code")
    result = await run_python_metric(code, context)
    if not result.ok:
        return MetricOutcome(metric_id, version, "error", None, [], result.rationale)
    return MetricOutcome(metric_id, version, result.status, result.value, [], result.rationale)
