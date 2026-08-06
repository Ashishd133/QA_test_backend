"""B2-03 end-to-end smoke check for `LatencyClock` (not the accuracy gate).

This runs the caller against a tiny synthetic target that waits a known
delay after the caller stops talking, then reports the clock's measured
latency against that injected delay. It is NOT asserted to ±75ms here: a
real LiveKit round-trip carries VAD's own stop_secs confirmation lag
(~200ms) plus WebRTC jitter between "caller stops talking" and "target's
turn-detector even starts counting" -- overhead inherent to any live audio
round-trip, unrelated to whether the clock's own arithmetic is correct.
That accuracy is validated deterministically instead, in
`tests/test_latency_clock.py`, by feeding synthetic frames with known
timestamps straight into the clock. This script exists to catch wiring
regressions (wrong frame classes, dead observers, etc.) by eyeballing that
the measured number is in the right ballpark.

Run with `uv run python -m app.engine.caller.latency_harness`.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import numpy as np
from dotenv import load_dotenv
from livekit import api
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    Frame,
    OutputAudioRawFrame,
    StartFrame,
    TTSSpeakFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.google.tts import GoogleTTSService
from pipecat.transports.livekit.transport import LiveKitParams, LiveKitTransport
from pipecat.workers.runner import WorkerRunner

from app.engine.caller.latency_clock import LatencyClock

load_dotenv()

INJECTED_DELAY_MS = 600
CAPTURE_TIMEOUT_SECS = 60.0
ECHO_SAMPLE_RATE = 16000
CHUNK_MS = 40


def _generate_tone(duration_secs: float, sample_rate: int, freq_hz: float = 440.0) -> bytes:
    t = np.linspace(0, duration_secs, int(sample_rate * duration_secs), endpoint=False)
    samples = (12000 * np.sin(2 * np.pi * freq_hz * t)).astype(np.int16)
    return samples.tobytes()


class DelayedEcho(FrameProcessor):
    """Waits `delay_ms` after the caller stops talking, then plays a fixed
    tone once -- not a TTS reply, since live TTS synthesis time itself would
    add uncontrolled variance on top of `delay_ms`, defeating the point of
    injecting a *known* delay to validate `LatencyClock`'s accuracy against.

    Gates on `VADUserStoppedSpeakingFrame`, not the plain
    `UserStoppedSpeakingFrame` -- `VADProcessor` (the only VAD integration
    path for the LiveKit transport; unlike Daily, its `TransportParams` has
    no `vad_analyzer` field) only ever emits the VAD-prefixed frames.
    """

    def __init__(self, delay_ms: int, *, sample_rate: int, tone: bytes, chunk_bytes: int) -> None:
        super().__init__()
        self._delay_ms = delay_ms
        self._sample_rate = sample_rate
        self._tone = tone
        self._chunk_bytes = chunk_bytes
        self._replied = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if isinstance(frame, VADUserStoppedSpeakingFrame) and not self._replied:
            self._replied = True
            self.create_task(self._reply_after_delay())

    async def _reply_after_delay(self) -> None:
        await asyncio.sleep(self._delay_ms / 1000)
        for i in range(0, len(self._tone), self._chunk_bytes):
            chunk = self._tone[i : i + self._chunk_bytes]
            await self.push_frame(
                OutputAudioRawFrame(audio=chunk, sample_rate=self._sample_rate, num_channels=1)
            )


class OneShotSpeaker(FrameProcessor):
    """Speaks `text` once, `grace_secs` after the pipeline starts.

    Not gated on `ClientConnectedFrame`: with both the caller and the echo
    target running as workers in this one process (rather than separate
    processes, as B2-02's script uses), whichever transport connects to the
    room second never sees its own "participant connected" event fire for
    the side that was already there when it started listening -- a fixed
    grace delay sidesteps that race entirely.
    """

    def __init__(self, text: str, *, grace_secs: float = 6.0) -> None:
        super().__init__()
        self._text = text
        self._grace_secs = grace_secs
        self._spoken = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if isinstance(frame, StartFrame) and not self._spoken:
            self._spoken = True
            self.create_task(self._speak_after_grace())

    async def _speak_after_grace(self) -> None:
        await asyncio.sleep(self._grace_secs)
        await self.push_frame(TTSSpeakFrame(text=self._text))


def _token(api_key: str, api_secret: str, room: str, identity: str) -> str:
    grants = api.VideoGrants(room_join=True, room=room)
    return api.AccessToken(api_key, api_secret).with_identity(identity).with_grants(grants).to_jwt()


async def _run_echo_target(url: str, api_key: str, api_secret: str, room: str) -> None:
    token = _token(api_key, api_secret, room, "latency-echo-target")
    transport = LiveKitTransport(
        url=url,
        token=token,
        room_name=room,
        params=LiveKitParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_out_sample_rate=ECHO_SAMPLE_RATE,
        ),
    )
    vad = VADProcessor(vad_analyzer=SileroVADAnalyzer())
    tone = _generate_tone(1.0, ECHO_SAMPLE_RATE)
    chunk_bytes = int(ECHO_SAMPLE_RATE * (CHUNK_MS / 1000)) * 2  # int16 mono
    echo = DelayedEcho(
        INJECTED_DELAY_MS, sample_rate=ECHO_SAMPLE_RATE, tone=tone, chunk_bytes=chunk_bytes
    )
    pipeline = Pipeline([transport.input(), vad, echo, transport.output()])
    await WorkerRunner().run(PipelineWorker(pipeline))


async def _run_caller(
    url: str, api_key: str, api_secret: str, room: str, creds_path: str, clock: LatencyClock
) -> None:
    token = _token(api_key, api_secret, room, "latency-caller")
    transport = LiveKitTransport(
        url=url,
        token=token,
        room_name=room,
        params=LiveKitParams(audio_in_enabled=True, audio_out_enabled=True),
    )
    tts = GoogleTTSService(
        credentials_path=creds_path,
        settings=GoogleTTSService.Settings(voice="en-US-Chirp3-HD-Puck"),
    )
    vad = VADProcessor(vad_analyzer=SileroVADAnalyzer())
    speaker = OneShotSpeaker("Hello, testing latency.")
    pipeline = Pipeline([transport.input(), vad, speaker, tts, transport.output()])
    await WorkerRunner().run(PipelineWorker(pipeline, observers=[clock]))


async def main() -> None:
    url = os.environ["LIVEKIT_URL"]
    api_key = os.environ["LIVEKIT_API_KEY"]
    api_secret = os.environ["LIVEKIT_API_SECRET"]
    creds_path = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
    room = f"latency-harness-{uuid.uuid4().hex[:8]}"

    clock = LatencyClock()
    logger.info(f"room={room!r} injected_delay_ms={INJECTED_DELAY_MS}")

    echo_task = asyncio.create_task(_run_echo_target(url, api_key, api_secret, room))
    caller_task = asyncio.create_task(
        _run_caller(url, api_key, api_secret, room, creds_path, clock)
    )

    deadline = asyncio.get_event_loop().time() + CAPTURE_TIMEOUT_SECS
    while not clock.turn_latencies and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.5)

    for task in (echo_task, caller_task):
        task.cancel()
    await asyncio.gather(echo_task, caller_task, return_exceptions=True)

    if not clock.turn_latencies:
        logger.error("no latency measurement captured within the deadline")
        raise SystemExit(1)

    result = clock.turn_latencies[0]
    overhead_ms = result.latency_ms - INJECTED_DELAY_MS
    logger.info(
        f"injected={INJECTED_DELAY_MS}ms measured={result.latency_ms:.1f}ms "
        f"overhead={overhead_ms:.1f}ms (VAD + network -- not clock error; "
        "accuracy is asserted in tests/test_latency_clock.py, not here)"
    )


if __name__ == "__main__":
    asyncio.run(main())
