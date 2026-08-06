"""B2-05: incremental + final judge passes.

Both passes call Gemini (Vertex AI, temperature 0 -- spine: "incremental
per-turn judge (cheap, temperature 0)") for structured verdicts, retrying
once on a malformed/unparseable response before raising `JudgeOutputError`,
and wrapping every call in an OTel span (prompt version + prompt + verdict
as span attributes) -- spine: "every judge call traced to Phoenix with
prompt version". Registering an actual OTLP exporter pointed at a Phoenix
collector is B2-08's job (run-scoped observability); absent one, these spans
are simply no-ops, but no code here needs to change once one exists.
"""

from __future__ import annotations

from typing import Protocol

from google import genai
from google.auth.credentials import Credentials
from google.genai import types
from opentelemetry import trace
from pydantic import BaseModel

from app.engine.judge.models import AssertionSpec, FinalVerdict, IncrementalVerdict, TranscriptTurn
from app.engine.judge.prompts import (
    FINAL_PROMPT_VERSION,
    INCREMENTAL_PROMPT_VERSION,
    render_final_prompt,
    render_incremental_prompt,
)

DEFAULT_MODEL = "gemini-2.5-flash"
MAX_RETRIES = 1  # one retry on malformed JSON before giving up

_tracer = trace.get_tracer(__name__)


class JudgeOutputError(RuntimeError):
    """Raised when the judge's structured output is unparseable after retries."""


class _GenerateContentResponse(Protocol):
    """Only the two fields `_generate_verdict` reads off a real
    `genai.types.GenerateContentResponse` -- kept minimal so a test fake can
    satisfy this structurally without constructing the real (much larger)
    response type."""

    parsed: object
    text: str | None


class _AioModels(Protocol):
    async def generate_content(
        self, *, model: str, contents: str, config: types.GenerateContentConfig
    ) -> _GenerateContentResponse: ...


class _Aio(Protocol):
    models: _AioModels


class GenAIClient(Protocol):
    """The slice of `google.genai.Client` the judge actually calls -- real
    clients satisfy this structurally, and tests can pass a lightweight fake
    without subclassing the real (heavyweight, network-backed) client.

    Plain attribute declarations, not read-only `@property`: the real
    `genai.Client.aio`/`AsyncClient.models` ARE read-only properties, which
    doesn't structurally match a plain-attribute Protocol either way (mypy
    still flags it -- see the ignore at this module's one real-client call
    site) -- but a `@property` here breaks the *test* fakes instead, since a
    `@dataclass` subclass can't assign to an inherited read-only property.
    Plain attributes are the only option that lets fakes be simple
    dataclasses without also being correct for the real client; pick the
    option that keeps the fakes actually working."""

    aio: _Aio


def build_vertex_client(*, credentials: Credentials, project: str, location: str) -> genai.Client:
    return genai.Client(vertexai=True, credentials=credentials, project=project, location=location)


async def _generate_verdict[VerdictT: BaseModel](
    client: GenAIClient,
    *,
    model: str,
    prompt: str,
    response_model: type[VerdictT],
    prompt_version: str,
    span_name: str,
) -> VerdictT:
    last_error: Exception | None = None
    attempt_prompt = prompt
    for attempt in range(MAX_RETRIES + 1):
        with _tracer.start_as_current_span(span_name) as span:
            span.set_attribute("judge.prompt_version", prompt_version)
            span.set_attribute("judge.attempt", attempt)
            span.set_attribute("judge.prompt", attempt_prompt)
            response = await client.aio.models.generate_content(
                model=model,
                contents=attempt_prompt,
                config=types.GenerateContentConfig(
                    temperature=0,
                    response_mime_type="application/json",
                    response_schema=response_model,
                ),
            )
            parsed = response.parsed
            if isinstance(parsed, response_model):
                span.set_attribute("judge.verdict", response.text or "")
                return parsed

            last_error = JudgeOutputError(f"unparseable judge output: {response.text!r}")
            span.set_attribute("judge.error", str(last_error))
            attempt_prompt = (
                f"{prompt}\n\nYour previous response was not valid JSON matching the "
                "required schema. Return ONLY a single JSON object matching it, no prose."
            )

    raise JudgeOutputError(
        f"judge output invalid after {MAX_RETRIES + 1} attempt(s): {last_error}"
    ) from last_error


class IncrementalJudge:
    """One live pass per turn: flips assertions the transcript newly
    resolves and moves the running score. See `AssertionState` for the
    per-assertion status this needs as input."""

    def __init__(self, client: GenAIClient, *, model: str = DEFAULT_MODEL) -> None:
        self._client = client
        self._model = model

    async def evaluate(
        self,
        assertions: list[AssertionSpec],
        transcript: list[TranscriptTurn],
        statuses: dict[str, str],
    ) -> IncrementalVerdict:
        prompt = render_incremental_prompt(assertions, transcript, statuses)
        return await _generate_verdict(
            self._client,
            model=self._model,
            prompt=prompt,
            response_model=IncrementalVerdict,
            prompt_version=INCREMENTAL_PROMPT_VERSION,
            span_name="judge.incremental",
        )


class FinalJudge:
    """One holistic pass at call end: resolves every assertion (including
    ones the incremental pass never reached), a final score, and sentiment."""

    def __init__(self, client: GenAIClient, *, model: str = DEFAULT_MODEL) -> None:
        self._client = client
        self._model = model

    async def evaluate(
        self, assertions: list[AssertionSpec], transcript: list[TranscriptTurn]
    ) -> FinalVerdict:
        prompt = render_final_prompt(assertions, transcript)
        return await _generate_verdict(
            self._client,
            model=self._model,
            prompt=prompt,
            response_model=FinalVerdict,
            prompt_version=FINAL_PROMPT_VERSION,
            span_name="judge.final",
        )
