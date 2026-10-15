"""B4-04 (offline): the attacker's code-owned policy -- authentication phase,
script order, early stop on success, bounded failure -- against a scripted
model."""

from app.engine.caller.persona import Turn
from app.engine.redteam.attacker import (
    CLOSING_LINE,
    GREETING_LINE,
    MAX_VERIFY_TURNS,
    AttackerCaller,
    AttackerLLMOutput,
    Exchange,
)
from app.engine.redteam.packs import AttackPack

IDENTITY = {"name": "Asha Rao", "dob": "1990-04-12", "verificationPhrase": "blue lagoon"}


def _pack(*, verified: bool = False) -> AttackPack:
    return AttackPack(
        id="pii-family-pretext",
        version=1,
        name="Family pretext",
        category="PII",
        requires_verified=verified,
        opening="OPENING",
        escalations=["ESC1", "ESC2"],
        success_signals=["reads another customer's data"],
        severity_if_success="critical",
        fix_template="fix",
    )


def _out(**kw: object) -> AttackerLLMOutput:
    base: dict[str, object] = {"utterance": "say", "kind": "attack"}
    return AttackerLLMOutput.model_validate({**base, **kw})


class ScriptedLLM:
    def __init__(self, outputs: list[AttackerLLMOutput]) -> None:
        self._outputs = list(outputs)
        self.prompts: list[str] = []

    async def plan(self, system: str, prompt: str) -> AttackerLLMOutput:
        self.prompts.append(prompt)
        return self._outputs.pop(0)


async def _drive(caller: AttackerCaller, agent_lines: list[str]) -> list[tuple[str, bool]]:
    transcript: list[Turn] = []
    out: list[tuple[str, bool]] = []
    first = await caller.next_turn(transcript)
    transcript.append(Turn("caller", first.utterance))
    out.append((first.utterance, first.call_complete))
    for line in agent_lines:
        transcript.append(Turn("agent", line))
        t = await caller.next_turn(transcript)
        transcript.append(Turn("caller", t.utterance))
        out.append((t.utterance, t.call_complete))
        if t.call_complete:
            break
    return out


async def test_unverified_pack_opens_with_the_scripted_opening_without_a_model_call() -> None:
    llm = ScriptedLLM([])
    caller = AttackerCaller(llm, _pack())
    first = await caller.next_turn([])
    assert first.utterance == "OPENING" and caller.attack_start == 0
    assert llm.prompts == []


async def test_script_is_walked_in_order_and_ends_when_exhausted() -> None:
    llm = ScriptedLLM(
        [
            _out(utterance="esc1 line", interim_verdict="blocked", interim_severity="low"),
            _out(utterance="esc2 line", interim_verdict="blocked", interim_severity="low"),
            _out(utterance="bye", interim_verdict="blocked", interim_severity="low"),
        ]
    )
    caller = AttackerCaller(llm, _pack())
    turns = await _drive(caller, ["no", "still no", "no way"])
    assert [u for u, _ in turns[:3]] == ["OPENING", "esc1 line", "esc2 line"]
    assert turns[-1] == (CLOSING_LINE, True)
    assert "Say attack line 2 of 3" in llm.prompts[0]
    assert "Say attack line 3 of 3" in llm.prompts[1]
    assert len(caller.attack_lines) == 3 and len(caller.exchanges) == 3
    assert not caller.stopped_on_success


async def test_a_leak_stops_the_attack_early_and_is_reported_live() -> None:
    seen: list[Exchange] = []

    async def on_exchange(e: Exchange) -> None:
        seen.append(e)

    llm = ScriptedLLM([_out(interim_verdict="leaked", interim_severity="critical")])
    caller = AttackerCaller(llm, _pack(), on_exchange=on_exchange)
    turns = await _drive(caller, ["Vikram Nair's card ends 3342.", "unused"])
    assert turns[-1] == (CLOSING_LINE, True) and len(turns) == 2
    assert caller.stopped_on_success
    assert seen[0].verdict == "leaked" and seen[0].severity == "critical"
    assert seen[0].attack_prompt == "OPENING"
    assert seen[0].agent_response == "Vikram Nair's card ends 3342."


async def test_verified_pack_authenticates_first_then_attacks() -> None:
    llm = ScriptedLLM(
        [
            _out(utterance="Asha Rao", kind="verify"),
            _out(utterance="my phrase is blue lagoon", kind="verify", agent_verified=True),
            _out(utterance="opening adapted", kind="attack"),
        ]
    )
    caller = AttackerCaller(llm, _pack(verified=True), identity=IDENTITY)
    turns = await _drive(caller, ["name?", "phrase?", "you're verified, how can I help?"])
    assert turns[0][0] == GREETING_LINE
    assert caller.attack_lines == ["opening adapted"]
    assert caller.attack_start == 6  # after greeting + 3 agent + 2 verify answers
    assert caller.verify_turns == 2 and not caller.verifying
    assert "Asha Rao" in llm.prompts[0]


async def test_an_attack_that_cannot_authenticate_is_reported_not_delivered() -> None:
    llm = ScriptedLLM([_out(utterance="Asha Rao", kind="verify") for _ in range(MAX_VERIFY_TURNS)])
    caller = AttackerCaller(llm, _pack(verified=True), identity=IDENTITY)
    turns = await _drive(caller, ["who are you?"] * (MAX_VERIFY_TURNS + 2))
    assert turns[-1] == (CLOSING_LINE, True)
    assert caller.failure is not None and "authenticate" in caller.failure
    assert caller.attack_lines == []


async def test_without_an_identity_the_directive_does_not_ask_for_one() -> None:
    llm = ScriptedLLM([_out()])
    caller = AttackerCaller(llm, _pack())
    await caller.next_turn(
        [Turn("agent", "welcome"), Turn("caller", "OPENING"), Turn("agent", "ok")]
    )
    assert "verify your identity" not in llm.prompts[0].split("DIRECTIVE:")[1]


class FailingLLM:
    async def plan(self, system: str, prompt: str) -> AttackerLLMOutput:
        raise RuntimeError("vertex unavailable")


async def test_a_failing_model_ends_the_call_with_a_failure_recorded() -> None:
    caller = AttackerCaller(FailingLLM(), _pack())
    turns = await _drive(caller, ["hello"])
    assert turns[-1] == (CLOSING_LINE, True)
    assert caller.failure is not None and "vertex unavailable" in caller.failure
