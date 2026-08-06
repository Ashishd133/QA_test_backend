"""B2-03: LatencyClock accuracy, validated deterministically.

A real LiveKit round-trip can't validate accuracy to +-75ms: VAD's own
stop_secs confirmation lag (~200ms) plus WebRTC jitter sit between "caller
stops talking" and "target's turn-detector even starts its clock", and that
overhead alone blows the tolerance regardless of how the clock's arithmetic
performs. That overhead is inherent to any real audio round-trip, not a bug
in the clock -- so accuracy is validated here by feeding synthetic
`FramePushed` events (known frame, known timestamp) straight into
`on_push_frame`, isolating the clock's own timestamp-subtraction and
silence-skip logic from transport/VAD variance. The live harness
(`app.engine.caller.latency_harness`) remains a manual end-to-end smoke
check that the wiring produces a plausible number, not an accuracy gate.
"""

import numpy as np
import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    UserAudioRawFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import FramePushed
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.engine.caller.latency_clock import LatencyClock

MS = 1_000_000  # nanoseconds per millisecond


class _StubProcessor(FrameProcessor):
    pass


def _pushed(frame: Frame, timestamp_ns: int) -> FramePushed:
    return FramePushed(
        source=_StubProcessor(),
        destination=_StubProcessor(),
        frame=frame,
        direction=FrameDirection.DOWNSTREAM,
        timestamp=timestamp_ns,
    )


def _audio_frame(rms: float) -> UserAudioRawFrame:
    samples = np.full(160, rms, dtype=np.int16)
    return UserAudioRawFrame(audio=samples.tobytes(), sample_rate=16000, num_channels=1)


async def test_measures_latency_between_caller_stop_and_first_loud_audio() -> None:
    clock = LatencyClock()
    t0 = 1_000 * MS

    await clock.on_push_frame(_pushed(BotStoppedSpeakingFrame(), t0))
    await clock.on_push_frame(_pushed(_audio_frame(600), t0 + 600 * MS))

    assert len(clock.turn_latencies) == 1
    result = clock.turn_latencies[0]
    assert result.latency_ms == pytest.approx(600, abs=1)
    assert not result.flagged
    assert result.flag_reason is None


async def test_flags_latency_over_threshold() -> None:
    clock = LatencyClock(threshold_ms=1000)
    t0 = 0

    await clock.on_push_frame(_pushed(BotStoppedSpeakingFrame(), t0))
    await clock.on_push_frame(_pushed(_audio_frame(600), t0 + 1200 * MS))

    result = clock.turn_latencies[0]
    assert result.latency_ms == pytest.approx(1200, abs=1)
    assert result.flagged
    assert result.flag_reason is not None


async def test_silent_frames_are_skipped_until_real_signal() -> None:
    clock = LatencyClock(silence_rms=300)
    t0 = 0

    await clock.on_push_frame(_pushed(BotStoppedSpeakingFrame(), t0))
    # Below the noise floor -- should not resolve the measurement.
    await clock.on_push_frame(_pushed(_audio_frame(50), t0 + 100 * MS))
    await clock.on_push_frame(_pushed(_audio_frame(20), t0 + 300 * MS))
    assert clock.turn_latencies == []

    # First frame that actually clears the noise floor resolves it.
    await clock.on_push_frame(_pushed(_audio_frame(600), t0 + 500 * MS))
    assert len(clock.turn_latencies) == 1
    assert clock.turn_latencies[0].latency_ms == pytest.approx(500, abs=1)


async def test_counts_interruption_when_bot_starts_speaking_over_agent() -> None:
    clock = LatencyClock()
    t0 = 0

    await clock.on_push_frame(_pushed(VADUserStartedSpeakingFrame(), t0))
    await clock.on_push_frame(_pushed(BotStartedSpeakingFrame(), t0 + 100 * MS))

    assert clock.interruption_count == 1


async def test_no_interruption_when_turns_do_not_overlap() -> None:
    clock = LatencyClock()
    t0 = 0

    await clock.on_push_frame(_pushed(VADUserStartedSpeakingFrame(), t0))
    await clock.on_push_frame(_pushed(VADUserStoppedSpeakingFrame(), t0 + 100 * MS))
    await clock.on_push_frame(_pushed(BotStartedSpeakingFrame(), t0 + 200 * MS))

    assert clock.interruption_count == 0
