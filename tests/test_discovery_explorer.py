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
    topics = [f"Topic {i}" for i in range(10)]  # enough that stalls can't end it first
    caller = ExplorerCaller(llm, identity=IDENTITY, candidates=topics, turn_budget=4)
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


async def test_intent_reply_is_attributed_to_the_topic_we_asked_about() -> None:
    """The model names the state its own way and never sets reached_candidate
    (what happened against the live reference agent). The topic is still
    credited, the node takes the candidate's name, and it is not asked twice."""
    llm = ScriptedLLM(
        [
            _out(_obs("provide_balance_and_ask_next", "intent")),
            _out(_obs("branch_hours_answer", "intent")),
        ]
    )
    caller = ExplorerCaller(llm, identity=IDENTITY, candidates=["Check balance", "Branch hours"])
    turns = await _drive(caller, ["your balance is 5", "we open at 9"])

    assert caller.reached == ["Check balance", "Branch hours"]
    assert [n.label for n in caller.graph.nodes] == ["Check balance", "Branch hours"]
    assert [n.key for n in caller.graph.nodes] == ["check_balance", "branch_hours"]
    assert "ASK about: 'Branch hours'" in llm.prompts[1]  # not 'Check balance' again
    assert turns[-1] == (CLOSING_LINE, True)


async def test_a_topic_the_agent_keeps_deflecting_is_abandoned_not_repeated_forever() -> None:
    llm = ScriptedLLM(
        [_out(_obs("main_menu", "menu")) for _ in range(2)] + [_out(_obs("hours", "intent"))]
    )
    caller = ExplorerCaller(llm, identity=IDENTITY, candidates=["Mortgages", "Hours"])
    await _drive(caller, ["anything else?", "anything else?", "we open at 9"])
    assert caller.unexplored == ["Mortgages"]
    assert caller.reached == ["Hours"]
    assert "ASK about: 'Hours'" in llm.prompts[2]


async def test_identity_questions_do_not_count_against_the_topic() -> None:
    gate = [_out(_obs(k, "gate", "pending")) for k in ("ask_name", "ask_dob", "ask_phrase")]
    llm = ScriptedLLM([*gate, _out(_obs("x", "intent"))])
    caller = ExplorerCaller(llm, identity=IDENTITY, candidates=["Check balance"])
    await _drive(caller, ["name?", "dob?", "phrase?", "your balance is 5"])
    assert caller.unexplored == [] and caller.reached == ["Check balance"]
    # One gate node, however many questions the model keyed separately.
    assert [n.kind for n in caller.graph.nodes].count("gate") == 1


async def test_spontaneous_handoff_is_attributed_to_the_handoff_topic() -> None:
    llm = ScriptedLLM([_out(_obs("escalation", "handoff"))])
    caller = ExplorerCaller(
        llm, identity=IDENTITY, candidates=["Check balance", "Talk to a human agent"]
    )
    await _drive(caller, ["connecting you to a specialist"])
    assert caller.reached == ["Talk to a human agent"]
    assert caller.remaining == ["Check balance"]


async def test_directive_names_the_following_topic_so_a_just_answered_one_is_not_repeated() -> None:
    llm = ScriptedLLM([_out(_obs("m", "menu"))])
    caller = ExplorerCaller(llm, identity=IDENTITY, candidates=["Check balance", "Branch hours"])
    await _drive(caller, ["hello"])
    prompt = llm.prompts[0]
    assert "just carried out 'Check balance'" in prompt
    assert "instead ASK about: 'Branch hours'" in prompt
    assert "Otherwise ASK about: 'Check balance'" in prompt


class FailingLLM:
    async def plan(self, system: str, prompt: str) -> ExplorerLLMOutput:
        raise RuntimeError("vertex unavailable")

    async def propose(self, system: str, prompt: str) -> ProposedCandidates:
        raise RuntimeError("vertex unavailable")


async def test_a_failing_model_call_ends_the_call_instead_of_hanging_it() -> None:
    caller = ExplorerCaller(FailingLLM(), identity=IDENTITY, candidates=["Check balance"])
    turns = await _drive(caller, ["hello"])
    assert turns == [(CLOSING_LINE, True)]
    assert caller.failure is not None and "vertex unavailable" in caller.failure


async def test_a_hung_model_call_times_out(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    from app.engine.discovery import explorer as explorer_module

    class HangingLLM(FailingLLM):
        async def plan(self, system: str, prompt: str) -> ExplorerLLMOutput:
            await asyncio.sleep(60)
            raise AssertionError("unreachable")

    monkeypatch.setattr(explorer_module, "PLAN_TIMEOUT_SECS", 0.05)
    caller = ExplorerCaller(HangingLLM(), identity=IDENTITY, candidates=["Check balance"])
    assert await _drive(caller, ["hello"]) == [(CLOSING_LINE, True)]
    assert caller.failure is not None


async def test_a_topic_that_merely_mentions_agent_does_not_end_exploration() -> None:
    """An insurance bot's real topic is "Find a local agent"; keyword-matching
    it as a human handoff would stop discovery after the first intent."""
    llm = ScriptedLLM([_out(_obs("x", "intent")), _out(_obs("y", "intent"))])
    caller = ExplorerCaller(
        llm, identity=IDENTITY, candidates=["Get a quote", "Find a local agent"]
    )
    turns = await _drive(caller, ["your quote is 5", "agents near you: ..."])
    assert caller.reached == ["Get a quote", "Find a local agent"]
    assert not caller.handed_off
    assert [n.kind for n in caller.graph.nodes] == ["intent", "intent"]
    assert turns[0][1] is False  # first intent did not end the call


async def test_reaching_a_topic_again_lands_on_its_existing_node() -> None:
    llm = ScriptedLLM(
        [
            _out(_obs("balance_a", "intent"), reached="Check balance"),
            _out(_obs("balance_b", "intent"), reached="Check balance"),
        ]
    )
    caller = ExplorerCaller(llm, identity=IDENTITY, candidates=["Check balance", "Branch hours"])
    await _drive(caller, ["your balance is 5", "as I said, 5"])
    assert [n.key for n in caller.graph.nodes] == ["check_balance"]
