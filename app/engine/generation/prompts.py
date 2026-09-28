"""B2.7-12: Jinja2 rendering for the scenario-generation prompt, same
StrictUndefined/versioning convention as app.engine.judge.prompts."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

GENERATE_PROMPT_VERSION = "generate-v1"

_env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_generate_prompt(
    *,
    source_label: str,
    context_text: str,
    persona_names: list[str],
    metric_names: list[str],
    count: int,
) -> str:
    template = _env.get_template("generate.jinja2")
    return template.render(
        source_label=source_label,
        context_text=context_text,
        persona_names=persona_names,
        metric_names=metric_names,
        count=count,
    )
