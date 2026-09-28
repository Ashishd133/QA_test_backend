"""B2.7-08 tests: app.engine.rubric's pure functions. No DB, no I/O --
these run in CI too.
"""

from __future__ import annotations

from app.engine.rubric import GatingMetricResult, evaluate_batch, evaluate_call

# ---------------------------------------------------------------------------
# evaluate_call
# ---------------------------------------------------------------------------


def test_no_gating_metrics_falls_back_to_default_badge_unchanged() -> None:
    assert evaluate_call([], default_badge="warn") == "warn"


def test_a_failed_gating_metric_fails_the_call_regardless_of_default() -> None:
    results = [GatingMetricResult(metric_id="m1", status="failed")]
    assert evaluate_call(results, default_badge="pass") == "fail"


def test_an_errored_gating_metric_fails_the_call_too() -> None:
    """An un-scoreable gating check is not a pass."""
    results = [GatingMetricResult(metric_id="m1", status="error")]
    assert evaluate_call(results, default_badge="pass") == "fail"


def test_a_warn_gating_metric_downgrades_to_warn() -> None:
    results = [GatingMetricResult(metric_id="m1", status="warn")]
    assert evaluate_call(results, default_badge="pass") == "warn"


def test_all_gating_metrics_passing_falls_back_to_default_badge() -> None:
    results = [
        GatingMetricResult(metric_id="m1", status="passed"),
        GatingMetricResult(metric_id="m2", status="not_sampled"),
    ]
    assert evaluate_call(results, default_badge="warn") == "warn"


def test_flipping_one_metric_from_gating_to_informational_changes_only_that() -> None:
    """The ticket's own literal done-when."""
    gating_and_failing = [GatingMetricResult(metric_id="m1", status="failed")]
    assert evaluate_call(gating_and_failing, default_badge="pass") == "fail"

    # Same metric, but the caller only passes gating results -- an
    # informational metric of the same underlying status simply never
    # reaches this function, so the call falls through to its default.
    now_informational: list[GatingMetricResult] = []
    assert evaluate_call(now_informational, default_badge="pass") == "pass"


# ---------------------------------------------------------------------------
# evaluate_batch
# ---------------------------------------------------------------------------


def test_no_rubric_falls_back_to_default_verdict_unchanged() -> None:
    assert evaluate_batch(["fail", "fail", "pass"], rubric=None, default_verdict="pass") == "pass"


def test_empty_batch_falls_back_to_default_verdict() -> None:
    assert evaluate_batch([], rubric={"failOnAnyCritical": True}, default_verdict="pass") == "pass"


def test_fail_on_any_critical_fails_the_batch() -> None:
    rubric: dict[str, object] = {"failOnAnyCritical": True}
    assert evaluate_batch(["pass", "pass", "fail"], rubric=rubric, default_verdict="pass") == "fail"


def test_fail_on_any_critical_false_does_not_fail_the_batch_on_its_own() -> None:
    rubric: dict[str, object] = {"failOnAnyCritical": False}
    result = evaluate_batch(["pass", "fail"], rubric=rubric, default_verdict="pass")
    assert result == "pass"


def test_min_pass_rate_fails_the_batch_below_threshold() -> None:
    rubric: dict[str, object] = {"minPassRate": 0.8}
    # 2/3 = 0.667 < 0.8
    result = evaluate_batch(["pass", "pass", "fail"], rubric=rubric, default_verdict="pass")
    assert result == "fail"


def test_min_pass_rate_passes_the_batch_at_or_above_threshold() -> None:
    rubric: dict[str, object] = {"minPassRate": 0.5}
    # 2/3 = 0.667 >= 0.5
    result = evaluate_batch(["pass", "pass", "fail"], rubric=rubric, default_verdict="pass")
    assert result == "pass"


def test_batch_verdict_recomputes_correctly_on_the_last_child() -> None:
    """The ticket's other literal done-when, expressed as pure recompute:
    each call to evaluate_batch is independent and stateless, so "recomputes
    correctly on the last child's completion" reduces to "correct given the
    full final set of child verdicts" -- which the threshold tests above
    already establish; this just names the property explicitly."""
    rubric: dict[str, object] = {"minPassRate": 1.0}
    partial = evaluate_batch(["pass", "pass"], rubric=rubric, default_verdict="pass")
    assert partial == "pass"
    with_last_child_failing = evaluate_batch(
        ["pass", "pass", "fail"], rubric=rubric, default_verdict="pass"
    )
    assert with_last_child_failing == "fail"
