"""B5-02: explorer policy, against a scripted model. The model's
classification is replayed from hand-labeled observations; what's under
test is the code-owned policy -- ordering, gate retries, termination."""

from app.engine.caller.persona import Turn
from app.engine.discovery.explorer import (
    CLOSING_LINE,
    DEFAULT_CANDIDATES,
    ExplorerCaller,
    ExplorerLLMOutput,
    ProposedCandidates,
    order_candidates,
    propose_candidates,
)
from app.engine.discovery.models import GateResult, NodeKind, TurnObservation

IDENTITY = {"name": "Asha Rao", "dob": "1990-04-12", "verificationPhrase": "blue lagoon"}


def _obs(
    key: str, kind: NodeKind, gate: GateResult | None = None, reason: str | None = None
) -> TurnObservation:
    return TurnObservation(
        node_key=key,
        label=key.replace("_", " ").title(),
        kind=kind,
        gate_result=gate,
        reason=reason,
    )


class ScriptedLLM:
    def __init__(self, outputs: list[ExplorerLLMOutput]) -> None:
        self._outputs = list(outputs)
        self.prompts: list[str] = []

    async def plan(self, system: str, prompt: str) -> ExplorerLLMOutput:
        self.prompts.append(prompt)
        return self._outputs.pop(0)

    async def propose(self, system: str, prompt: str) -> ProposedCandidates:
        raise RuntimeError("planning unavailable")


def _out(obs: TurnObservation, said: str = "ok", reached: str | None = None) -> ExplorerLLMOutput:
    return ExplorerLLMOutput(observation=obs, utterance=said, reached_candidate=reached)


async def _drive(caller: ExplorerCaller, agent_lines: list[str]) -> list[tuple[str, bool]]:
    """Plays an agent that says each line in turn; stops when the explorer
    says it is done. Returns (caller line, complete) per caller turn."""
    transcript: list[Turn] = []
    turns: list[tuple[str, bool]] = []
    for line in agent_lines:
        transcript.append(Turn("agent", line))
        t = await caller.next_turn(transcript)
        transcript.append(Turn("caller", t.utterance))
        turns.append((t.utterance, t.call_complete))
        if t.call_complete:
            break
    return turns


async def test_opens_without_calling_the_model() -> None:
    llm = ScriptedLLM([])
    caller = ExplorerCaller(llm, identity=IDENTITY, candidates=["Balance"])
    first = await caller.next_turn([])
    assert not first.call_complete and llm.prompts == []


async def test_silence_nudges_without_spending_budget() -> None:
    llm = ScriptedLLM([])
    caller = ExplorerCaller(llm, identity=IDENTITY, candidates=["Balance"])
    t = await caller.next_turn([Turn("agent", "hi"), Turn("caller", "hello")])
    assert not t.call_complete and caller.caller_turns == 0 and llm.prompts == []


async def test_correct_identity_probes_in_order_and_stops_when_frontier_is_exhausted() -> None:
    llm = ScriptedLLM(
        [
            _out(_obs("greeting", "greeting")),
            _out(_obs("verification", "gate", "pending")),
            _out(_obs("main_menu", "menu", "passed")),
            _out(_obs("balance", "intent"), reached="Check balance"),
            _out(_obs("branch", "intent"), reached="Branch info"),
        ]
    )
    caller = ExplorerCaller(
        llm, identity=IDENTITY, candidates=["Talk to a human agent", "Check balance", "Branch info"]
    )
    turns = await _drive(caller, ["a", "b", "c", "d", "e"])

    assert caller.reached == ["Check balance", "Branch info"]
    # Human-handoff topic was ordered last, so it is what's left; not stopped yet.
    assert caller.remaining == ["Talk to a human agent"]
    assert "ASK about: 'Check balance'" in llm.prompts[0]
    assert "ASK about: 'Branch info'" in llm.prompts[4]
    assert not any(c for _, c in turns)


async def test_stops_when_every_candidate_is_reached() -> None:
    llm = ScriptedLLM(
        [
            _out(_obs("verification", "gate", "pending")),
            _out(_obs("balance", "intent", "passed"), reached="Check balance"),
        ]
    )
    caller = ExplorerCaller(llm, identity=IDENTITY, candidates=["Check balance"])
    turns = await _drive(caller, ["verify?", "your balance is 5"])
    assert turns[-1] == (CLOSING_LINE, True)


async def test_wrong_identity_gives_up_after_three_rejections() -> None:
    reason = "The agent said the details did not match."
    llm = ScriptedLLM(
        [_out(_obs("verification", "gate", "pending"))]
        + [_out(_obs("verification", "gate", "rejected", reason)) for _ in range(3)]
    )
    caller = ExplorerCaller(llm, identity=IDENTITY, candidates=["Check balance"])
    turns = await _drive(caller, ["id?", "no match", "no match", "no match", "never reached"])
    assert turns[-1] == (CLOSING_LINE, True)
    assert caller.rejections == 3 and len(turns) == 4
    gate = next(n for n in caller.graph.nodes if n.kind == "gate")
    assert gate.state == "blocked" and gate.blocked_reason == reason


async def test_handoff_ends_the_exploration() -> None:
    llm = ScriptedLLM([_out(_obs("human_handoff", "handoff"), reached="Talk to a human agent")])
    caller = ExplorerCaller(
        llm, identity=IDENTITY, candidates=["Talk to a human agent", "Check balance"]
    )
    turns = await _drive(caller, ["connecting you to a specialist"])
    assert turns == [(CLOSING_LINE, True)]


async def test_turn_budget_guarantees_termination() -> None:
    llm = ScriptedLLM([_out(_obs("main_menu", "menu")) for _ in range(20)])
    caller = ExplorerCaller(llm, identity=IDENTITY, candidates=["Never reached"], turn_budget=4)
    turns = await _drive(caller, ["anything else?"] * 20)
    assert len(turns) == 3 and turns[-1] == (CLOSING_LINE, True)


async def test_model_declaring_the_call_over_is_honoured() -> None:
    done = ExplorerLLMOutput(
        observation=_obs("call_end", "other"), utterance="bye", call_complete=True
    )
    caller = ExplorerCaller(ScriptedLLM([done]), identity=IDENTITY, candidates=["x"])
    assert await _drive(caller, ["goodbye"]) == [("bye", True)]


async def test_observations_are_reported_as_they_happen() -> None:
    seen: list[tuple[int, str, list[str]]] = []

    async def on_obs(index: int, obs: TurnObservation, result) -> None:  # type: ignore[no-untyped-def]
        seen.append((index, obs.node_key, result.changed))

    llm = ScriptedLLM(
        [_out(_obs("greeting", "greeting")), _out(_obs("verification", "gate", "pending"))]
    )
    caller = ExplorerCaller(llm, identity=IDENTITY, candidates=["x"], on_observation=on_obs)
    await _drive(caller, ["hello", "who are you?"])
    assert seen == [(0, "greeting", ["greeting"]), (2, "verification", ["verification"])]


def test_order_candidates_dedupes_and_puts_handoff_last() -> None:
    assert order_candidates(
        ["Speak to a representative", "Check balance", "check balance", "Block card"]
    ) == ["Check balance", "Block card", "Speak to a representative"]


async def test_planning_falls_back_to_defaults() -> None:
    llm = ScriptedLLM([])
    assert await propose_candidates(llm, name="A", description=None, prompt=None) == list(
        DEFAULT_CANDIDATES
    )
    # Has a description but the model call fails: still usable.
    assert await propose_candidates(llm, name="A", description="a bank bot", prompt=None) == list(
        DEFAULT_CANDIDATES
    )
