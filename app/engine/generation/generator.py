"""B2.7-12: LLM-driven scenario draft generation. Reuses B2-05's structured-
output call machinery (app.engine.judge.judge._generate_verdict -- retry
once on malformed JSON, OTel span per attempt, UsageTracker recording)
rather than a parallel implementation of the same Gemini structured-call
pattern."""

from __future__ import annotations

from app.engine.generation.models import GenerationResponse
from app.engine.generation.prompts import GENERATE_PROMPT_VERSION, render_generate_prompt
from app.engine.judge.judge import DEFAULT_MODEL, GenAIClient, _generate_verdict
from app.usage import UsageTracker


async def generate_drafts(
    client: GenAIClient,
    *,
    source_label: str,
    context_text: str,
    persona_names: list[str],
    metric_names: list[str],
    count: int,
    usage: UsageTracker,
    model: str = DEFAULT_MODEL,
) -> GenerationResponse:
    prompt = render_generate_prompt(
        source_label=source_label,
        context_text=context_text,
        persona_names=persona_names,
        metric_names=metric_names,
        count=count,
    )
    return await _generate_verdict(
        client,
        model=model,
        prompt=prompt,
        response_model=GenerationResponse,
        prompt_version=GENERATE_PROMPT_VERSION,
        span_name="generation.scenarios",
        usage=usage,
    )
