"""B2-05: Jinja2 rendering for the judge's versioned rubric prompts.

Templates live in `templates/` and are rendered with `StrictUndefined` so a
typo'd template variable fails loudly at render time instead of silently
rendering blank -- these prompts are scoring logic, not display copy.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from app.engine.judge.models import AssertionSpec, CompiledMetricSignal, TranscriptTurn

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

# Bumped whenever a template's scoring behavior changes -- traced alongside
# every judge call (spine: "every judge call traced to Phoenix with prompt
# version") so a verdict can always be tied back to the exact rubric that
# produced it.
INCREMENTAL_PROMPT_VERSION = "incremental-v1"
# B2.7-06/09: final.jinja2 gained conditional "## Metrics" and "## Goal"
# sections -- the version bumps because the template's own logic changed,
# even though render_final_prompt(assertions, transcript) (no metrics, no
# goal) still produces byte-identical output to final-v1 (see
# tests/test_metric_compilation.py's and tests/test_goal_evaluation.py's
# snapshot tests).
# B2.7-13: added an unconditional Instructions paragraph generalizing the
# fix for evals/cases/g_three_failed_verifications_auto_handoff.json's a2
# flakiness (a hand-labeled-failed case the judge sometimes scored
# "passed") -- a handoff/escalation only satisfies an obligation if the
# handoff itself is what was asked for, not a stand-in for an unresolved
# original request. Unconditional, so this doesn't change the "no goal/
# metrics -> byte-identical to final-v1" snapshot tests' own comparisons
# (all variants still match each other), just the shared text they compare.
FINAL_PROMPT_VERSION = "final-v4"

_env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_incremental_prompt(
    assertions: list[AssertionSpec],
    transcript: list[TranscriptTurn],
    statuses: dict[str, str],
) -> str:
    template = _env.get_template("incremental.jinja2")
    return template.render(assertions=assertions, transcript=transcript, statuses=statuses)


def render_final_prompt(
    assertions: list[AssertionSpec],
    transcript: list[TranscriptTurn],
    metrics: list[CompiledMetricSignal] | None = None,
    goal: str | None = None,
) -> str:
    template = _env.get_template("final.jinja2")
    return template.render(
        assertions=assertions, transcript=transcript, metrics=metrics or [], goal=goal
    )
