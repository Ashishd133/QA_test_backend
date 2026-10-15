"""B5-02: PersonaRunner's opt-in silence watchdog. The runner is a pipecat
FrameProcessor, so these drive its internals directly with `push_frame`
stubbed -- the pipeline wiring itself is covered by the live check."""

from dataclasses import dataclass

from pipecat.frames.frames import EndFrame, Frame, TTSSpeakFrame

from app.engine.caller.persona import Turn
from app.engine.caller.persona_call import PersonaRunner


@dataclass
class _Planned:
    utterance: str
    call_complete: bool = False


class _Planner:
    def __init__(self) -> None:
        self.calls = 0

    async def next_turn(self, transcript: list[Turn]) -> _Planned:
        self.calls += 1
        return _Planned(f"hello? ({self.calls})")


def _runner(**kwargs: object) -> tuple[PersonaRunner, list[Frame]]:
    pushed: list[Frame] = []
    runner = PersonaRunner(_Planner(), **kwargs)  # type: ignore[arg-type]

    async def push(frame: Frame, direction: object = None) -> None:
        pushed.append(frame)

    runner.push_frame = push  # type: ignore[method-assign]
    runner._schedule_silence_check = lambda: None  # type: ignore[method-assign]
    return runner, pushed


async def test_a_silent_agent_is_nudged_then_the_call_ends() -> None:
    runner, pushed = _runner(silence_timeout_secs=0.01, max_silent_nudges=2)
    await runner._check_silence(0)
    await runner._check_silence(0)
    assert [type(f) for f in pushed] == [TTSSpeakFrame, TTSSpeakFrame]
    assert runner.end_reason is None

    await runner._check_silence(0)  # third silence: out of nudges
    assert isinstance(pushed[-1], EndFrame)
    assert runner.end_reason == "timeout"


async def test_watchdog_is_off_by_default() -> None:
    runner = PersonaRunner(_Planner())
    runner._schedule_silence_check()  # must not need a running pipeline
    assert runner._pending_silence_check is None
