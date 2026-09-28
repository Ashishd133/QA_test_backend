"""B2.7-08: `suites.rubric` decides run colour -- which metrics gate a
call's verdict vs are purely informational, the batch's minimum pass
rate, whether any critical finding fails the whole batch. Pure functions,
no I/O, so both are unit-testable without a DB; the call sites
(app/engine/executor/simulation.py, app/workers/rollup.py) do the actual
querying and pass in already-fetched data.

Gating source, pinned down rather than left implicit: `scenario_metrics.
gating` (per-scenario, per-metric -- B2.7-11 exposes attaching it via the
API) is what this module reads for a *call's* verdict. `suites.rubric`
itself only carries batch-level knobs (`minPassRate`, `failOnAnyCritical`)
here -- a `gatingMetricIds` list on the rubric, if ever added, would be a
suite-level default applied where a scenario doesn't specify its own
gating, not implemented by this ticket's scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CallVerdict = Literal["pass", "warn", "fail"]
BatchVerdict = Literal["pass", "fail"]
MetricStatus = Literal["passed", "failed", "warn", "error", "not_sampled", "not_computable"]


@dataclass(frozen=True)
class GatingMetricResult:
    metric_id: str
    status: MetricStatus


def evaluate_call(
    gating_results: list[GatingMetricResult], *, default_badge: CallVerdict
) -> CallVerdict:
    """`default_badge` is `_badge_from_final_score`'s existing score-based
    result -- a scenario with no gating metrics at all falls back to it
    unchanged, bit-for-bit (this ticket supersedes, never regresses, the
    pre-existing score-only badge). A noisy *informational* (non-gating)
    metric never reaches this function at all -- callers only pass the
    gating subset -- so it can never fail a deploy gate, per the ticket's
    own named bar. `error` (a metric that failed to score at all) counts
    the same as `failed`: an un-scoreable gating check is not a pass.
    `warn`/`not_sampled`/`not_computable` never fail a gate on their own."""
    if any(r.status in ("failed", "error") for r in gating_results):
        return "fail"
    if any(r.status == "warn" for r in gating_results):
        return "warn"
    return default_badge


def evaluate_batch(
    child_verdicts: list[CallVerdict],
    *,
    rubric: dict[str, object] | None,
    default_verdict: BatchVerdict,
) -> BatchVerdict:
    """`default_verdict` is today's pre-rubric convention (see
    app/workers/rollup.py's own module docstring: "any child failed does
    NOT make the parent failed" is a deliberate existing choice, not a
    bug) -- a batch with no rubric configured, or with no terminal
    children at all, falls back to it unchanged. With a rubric:
    `failOnAnyCritical` fails the batch outright if any child's own
    verdict came back "fail"; `minPassRate` (0-1) fails it if the
    fraction of "pass" children falls below the threshold. Either knob
    alone can fail the batch; neither can un-fail it once the other has."""
    if not rubric or not child_verdicts:
        return default_verdict
    if rubric.get("failOnAnyCritical") and any(v == "fail" for v in child_verdicts):
        return "fail"
    min_pass_rate = rubric.get("minPassRate")
    if isinstance(min_pass_rate, int | float):
        pass_count = sum(1 for v in child_verdicts if v == "pass")
        if pass_count / len(child_verdicts) < min_pass_rate:
            return "fail"
    return default_verdict
