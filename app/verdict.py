"""Shared pass/warn/fail/idle verdict computation (first written for
B1-01's suite/scenario list, reused by B1-03's runs list/detail): this is
always a computed judgment derived from a run's raw status + metrics, never
a stored column (spine §3 has no such enum on `runs`)."""

from typing import Literal

Verdict = Literal["pass", "warn", "fail", "idle"]


def format_score(score: object) -> str:
    if isinstance(score, int | float):
        return f"{round(score * 100)}%"
    return "-"


def badge_from_score(score: float) -> Verdict:
    if score >= 0.8:
        return "pass"
    if score >= 0.5:
        return "warn"
    return "fail"


def verdict_for_run(run_status: str, metrics: dict[str, object] | None) -> Verdict:
    """Folds in-progress statuses (queued/claimed/running) into 'idle' --
    there's no verdict to show until the run actually concludes."""
    metrics = metrics or {}
    score = metrics.get("score")
    if run_status == "completed":
        badge = metrics.get("resultBadge")
        if badge in ("pass", "warn", "fail"):
            return badge
        if isinstance(score, int | float):
            return badge_from_score(score)
        return "pass"
    if run_status in ("failed", "cancelled"):
        return "fail"
    return "idle"
