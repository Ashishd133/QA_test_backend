"""B5-02: the explorer -- a `TurnPlanner` that maps an agent instead of
pursuing one goal.

Division of labor, on purpose: the LLM does the two things it is good at
(classify what the agent just said; phrase a natural line), while plain
code owns the policy -- which topic to probe next, how many times to retry
a rejected gate, and when to stop. That keeps termination guaranteed and
breadth-first ordering reproducible regardless of what the model does.

Policy:
  * Probe candidate topics in the order given, human-handoff last (a
    handoff usually ends the useful part of the call).
  * While a gate is pending/rejected the only job is to answer it with the
    supplied identity. A wrong-value identity is not an error: after
    `MAX_GATE_REJECTIONS` rejections we stop retrying and the gate is
    `blocked` with the agent's own reason.
  * Stop when: the agent hands off, the gate is exhausted, every candidate
    is reached, the model says the call is over, or the turn budget is hit
    (frontier exhaustion + budget = guaranteed termination).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from google import genai
from google.auth.credentials import Credentials
from google.genai import types
from pydantic import BaseModel

from app.engine.caller.persona import DEFAULT_MODEL, Turn
from app.engine.discovery.graph import FlowGraph, ObserveResult
from app.engine.discovery.models import TurnObservation
from app.engine.discovery.prompts import (
    EXPLORER_SYSTEM,
    EXPLORER_USER,
    PROPOSE_SYSTEM,
    PROPOSE_USER,
)

DEFAULT_TURN_BUDGET = 40
MAX_GATE_REJECTIONS = 3
MAX_CANDIDATES = 6
_HISTORY_TURNS = 14

OPENING_LINE = "Hi, I was hoping you could help me with something."
CLOSING_LINE = "Okay, thanks so much for your help. Goodbye."
_NUDGE_LINE = "Hello? Are you still there?"

# Used when no description exists to plan from, or the planning call fails.
DEFAULT_CANDIDATES = (
    "Check account balance",
    "Report a lost or stolen card",
    "Find branch hours and location",
    "Talk to a human agent",
)

_HANDOFF_WORDS = re.compile(
    r"\b(human|person|specialist|representative|agent|operator|advisor)\b", re.I
)


class ExplorerLLMOutput(BaseModel):
    observation: TurnObservation
    utterance: str
    call_complete: bool = False
    reached_candidate: str | None = None


class ProposedCandidates(BaseModel):
    topics: list[str]


class ExplorerLLM(Protocol):
    async def plan(self, system: str, prompt: str) -> ExplorerLLMOutput: ...

    async def propose(self, system: str, prompt: str) -> ProposedCandidates: ...


class GeminiExplorerLLM:
    def __init__(
        self, *, credentials: Credentials, project: str, location: str, model: str = DEFAULT_MODEL
    ) -> None:
        self._model = model
        self._client = genai.Client(
            vertexai=True, credentials=credentials, project=project, location=location
        )

    async def _generate[T: BaseModel](
        self, system: str, prompt: str, schema: type[T], *, temperature: float
    ) -> T:
        last: Exception | None = None
        for _ in range(2):  # one retry: structured output occasionally fails to parse
            try:
                response = await self._client.aio.models.generate_content(
                    model=self._model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system,
                        temperature=temperature,
                        response_mime_type="application/json",
                        response_schema=schema,
                    ),
                )
                if isinstance(response.parsed, schema):
                    return response.parsed
                last = ValueError(f"unparseable explorer output: {response.text!r}")
            except Exception as exc:  # noqa: BLE001 - retried, then re-raised below
                last = exc
        assert last is not None
        raise last

    async def plan(self, system: str, prompt: str) -> ExplorerLLMOutput:
        return await self._generate(system, prompt, ExplorerLLMOutput, temperature=0.3)

    async def propose(self, system: str, prompt: str) -> ProposedCandidates:
        return await self._generate(system, prompt, ProposedCandidates, temperature=0.2)


async def propose_candidates(
    llm: ExplorerLLM, *, name: str, description: str | None, prompt: str | None
) -> list[str]:
    """Plan the frontier up front. Falls back to `DEFAULT_CANDIDATES` when
    there is nothing to plan from or the call fails -- discovery must still
    run on an agent with no description."""
    if not (description or prompt):
        return list(DEFAULT_CANDIDATES)
    try:
        proposed = await llm.propose(
            PROPOSE_SYSTEM,
            PROPOSE_USER.format(
                name=name, description=description or "(none)", prompt=(prompt or "(none)")[:4000]
            ),
        )
    except Exception:  # noqa: BLE001 - planning is best-effort
        return list(DEFAULT_CANDIDATES)
    topics = [t.strip() for t in proposed.topics if t.strip()][:MAX_CANDIDATES]
    return topics or list(DEFAULT_CANDIDATES)


def order_candidates(candidates: Sequence[str]) -> list[str]:
    """De-duplicated, human-handoff topics last."""
    seen: set[str] = set()
    plain: list[str] = []
    handoff: list[str] = []
    for c in candidates:
        key = c.casefold()
        if key in seen:
            continue
        seen.add(key)
        (handoff if _HANDOFF_WORDS.search(c) else plain).append(c)
    return plain + handoff


@dataclass(frozen=True)
class ExplorerTurn:
    utterance: str
    call_complete: bool


OnObservation = Callable[[int, TurnObservation, ObserveResult], Awaitable[None]]


class ExplorerCaller:
    def __init__(
        self,
        llm: ExplorerLLM,
        *,
        identity: Mapping[str, object],
        candidates: Sequence[str],
        graph: FlowGraph | None = None,
        turn_budget: int = DEFAULT_TURN_BUDGET,
        on_observation: OnObservation | None = None,
    ) -> None:
        self._llm = llm
        self._identity = identity
        self.candidates = order_candidates(candidates)
        self.graph = graph if graph is not None else FlowGraph()
        self._turn_budget = turn_budget
        self._on_observation = on_observation
        self.reached: list[str] = []
        self.rejections = 0
        self.caller_turns = 0
        self.handed_off = False

    @property
    def remaining(self) -> list[str]:
        done = {r.casefold() for r in self.reached}
        return [c for c in self.candidates if c.casefold() not in done]

    async def next_turn(self, transcript: list[Turn]) -> ExplorerTurn:
        if not transcript:
            return ExplorerTurn(OPENING_LINE, False)
        if transcript[-1].speaker != "agent":
            # Agent has said nothing since our last line; don't burn a model
            # call or a turn of budget on silence.
            return ExplorerTurn(_NUDGE_LINE, False)

        self.caller_turns += 1
        out = await self._llm.plan(EXPLORER_SYSTEM, self._prompt(transcript))

        index = len(transcript) - 1
        result = self.graph.observe(index, out.observation)
        if self._on_observation is not None:
            await self._on_observation(index, out.observation, result)
        self._absorb(out)

        if out.call_complete:
            return ExplorerTurn(out.utterance, True)
        if self._should_stop(out.observation):
            return ExplorerTurn(CLOSING_LINE, True)
        return ExplorerTurn(out.utterance, False)

    def _absorb(self, out: ExplorerLLMOutput) -> None:
        obs = out.observation
        if obs.gate_result == "rejected":
            self.rejections += 1
        if obs.kind == "handoff":
            self.handed_off = True
        for claimed in (
            out.reached_candidate,
            obs.label if obs.kind in ("intent", "handoff") else None,
        ):
            if claimed is None:
                continue
            match = next((c for c in self.candidates if c.casefold() == claimed.casefold()), None)
            if match is not None and match not in self.reached:
                self.reached.append(match)

    def _gate_open(self, obs: TurnObservation) -> bool:
        return obs.kind == "gate" and obs.gate_result in ("pending", "rejected")

    def _should_stop(self, obs: TurnObservation) -> bool:
        if self.handed_off or self.rejections >= MAX_GATE_REJECTIONS:
            return True
        if self.caller_turns >= self._turn_budget - 1:
            return True
        return not self.remaining and not self._gate_open(obs)

    def _directive(self) -> str:
        """Decided before the model has classified the latest agent turn
        (same call), so it is conditional: the model picks the branch from
        its own observation. The stop decision after the call is code's."""
        if self.caller_turns >= self._turn_budget - 1 or not self.remaining:
            return "WRAP UP: thank the agent and say goodbye."
        return (
            "If the agent is asking you to verify your identity, or says your details did not "
            "match, ANSWER using only your identity details, giving just what it asked for. "
            f"Otherwise ASK about: {self.remaining[0]!r}, in one short natural sentence."
        )

    def _prompt(self, transcript: list[Turn]) -> str:
        lines = [
            f"{'You' if t.speaker == 'caller' else 'Agent'}: {t.text.strip()}"
            for t in transcript[-_HISTORY_TURNS:]
        ]
        known = (
            "\n".join(f"- {n.key}: {n.label} ({n.kind})" for n in self.graph.nodes) or "(none yet)"
        )
        return EXPLORER_USER.format(
            known_states=known,
            candidates=", ".join(self.candidates),
            reached=", ".join(self.reached) or "(none)",
            identity=", ".join(f"{k}: {v}" for k, v in self._identity.items()),
            rejections=self.rejections,
            turns_used=self.caller_turns - 1,
            turn_budget=self._turn_budget,
            conversation="\n".join(lines),
            directive=self._directive(),
        )
