"""B2-04: persona LLM -- scenario persona + goal -> next caller utterance.

Given the transcript so far, asks Gemini Flash (via Vertex AI, the same
service-account auth path as the reference agent's realtime model) to
produce the caller persona's next spoken line plus an end-of-call signal.
The turn budget cap lives in the caller (`persona_call.py`), not here: the
model's own call_complete judgment is a signal, not a hard limit, since a
misbehaving reference agent could otherwise keep the model talking forever.
"""

from __future__ import annotations

from dataclasses import dataclass

from google import genai
from google.auth.credentials import Credentials
from google.genai import types
from pydantic import BaseModel

DEFAULT_MODEL = "gemini-2.5-flash"

_SYSTEM_TEMPLATE = """You are {name}, a bank customer calling phone support. \
Your personality traits: {traits}.

Your goal for this call: {goal}

Stay in character at all times -- you are a real customer on a phone call, \
not an AI assistant. Speak naturally, in short conversational lines, the way \
a person actually talks on the phone (no bullet points, no long speeches).

Given the conversation so far, produce your next spoken line. Set \
call_complete to true once your goal has been met and the call is winding \
down naturally (e.g. you've thanked the agent and said goodbye) -- when \
call_complete is true, utterance should be that closing line itself, not a \
mid-call check-in.
"""


@dataclass(frozen=True)
class PersonaSpec:
    name: str
    traits: dict[str, str]
    goal: str
    opening_line: str


@dataclass(frozen=True)
class Turn:
    speaker: str  # "caller" | "agent"
    text: str


class PersonaTurnOutput(BaseModel):
    utterance: str
    call_complete: bool


def _build_prompt(transcript: list[Turn]) -> str:
    lines = [f"{'You' if t.speaker == 'caller' else 'Agent'}: {t.text}" for t in transcript]
    return "Conversation so far:\n" + "\n".join(lines) + "\n\nYour next line:"


class PersonaCaller:
    """Wraps a Vertex AI Gemini Flash client to drive one persona's side of a call."""

    def __init__(
        self,
        persona: PersonaSpec,
        *,
        credentials: Credentials,
        project: str,
        location: str,
        model: str = DEFAULT_MODEL,
    ) -> None:
        self._persona = persona
        self._model = model
        self._client = genai.Client(
            vertexai=True, credentials=credentials, project=project, location=location
        )
        self._system_instruction = _SYSTEM_TEMPLATE.format(
            name=persona.name, traits=persona.traits, goal=persona.goal
        )

    async def next_turn(self, transcript: list[Turn]) -> PersonaTurnOutput:
        if not transcript:
            return PersonaTurnOutput(utterance=self._persona.opening_line, call_complete=False)

        response = await self._client.aio.models.generate_content(
            model=self._model,
            contents=_build_prompt(transcript),
            config=types.GenerateContentConfig(
                system_instruction=self._system_instruction,
                temperature=0.6,
                response_mime_type="application/json",
                response_schema=PersonaTurnOutput,
            ),
        )
        parsed = response.parsed
        if not isinstance(parsed, PersonaTurnOutput):
            raise ValueError(f"persona LLM returned unparseable output: {response.text!r}")
        return parsed
