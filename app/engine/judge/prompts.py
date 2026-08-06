"""B2-05: Jinja2 rendering for the judge's versioned rubric prompts.

Templates live in `templates/` and are rendered with `StrictUndefined` so a
typo'd template variable fails loudly at render time instead of silently
rendering blank -- these prompts are scoring logic, not display copy.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from app.engine.judge.models import AssertionSpec, TranscriptTurn

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

# Bumped whenever a template's scoring behavior changes -- traced alongside
# every judge call (spine: "every judge call traced to Phoenix with prompt
# version") so a verdict can always be tied back to the exact rubric that
# produced it.
INCREMENTAL_PROMPT_VERSION = "incremental-v1"
FINAL_PROMPT_VERSION = "final-v1"

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


def render_final_prompt(assertions: list[AssertionSpec], transcript: list[TranscriptTurn]) -> str:
    template = _env.get_template("final.jinja2")
    return template.render(assertions=assertions, transcript=transcript)
