"""B2-02: caller runtime — transport + audio loop (no LLM yet).

A Pipecat pipeline that joins a LiveKit room as the synthetic caller: Google
TTS out, the target agent's audio in -> streaming Google STT (word timings)
-> VAD -> turn assembly. Runs a hardcoded 3-utterance script against whatever
agent auto-joins the room (the B2-01 reference agent, in dev) and logs the
transcript of what the agent said back.

Run with `uv run python -m app.engine.caller.scripted_call` while the
reference agent worker (`app.engine.reference_agent.agent dev`) is running
and registered with the same LiveKit project.
"""

from __future__ import annotations

import asyncio
import os
import uuid

from dotenv import load_dotenv
from livekit import api
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    ClientConnectedFrame,
    EndFrame,
    Frame,
    TranscriptionFrame,
    TTSSpeakFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.google.stt import GoogleSTTService
from pipecat.services.google.tts import GoogleTTSService
from pipecat.transports.livekit.transport import LiveKitParams, LiveKitTransport
from pipecat.workers.runner import WorkerRunner

load_dotenv()

SCRIPT = [
    "Hi, my name is Asha Rao. My date of birth is April 12th, 1990, and my "
    "security phrase is blue lagoon.",
    "Can you tell me my account balance?",
    "Thanks, that's all I needed.",
]

JOIN_GRACE_SECS = 5.0  # let the target agent finish joining + connecting before we speak
TURN_GAP_SECS = 1.5  # pause after the agent stops talking before our next line
RUN_TIMEOUT_SECS = 120.0


class ScriptRunner(FrameProcessor):
    """Speaks each line of `script`, advancing once the agent's transcript goes quiet.

    STT finalizes a transcript chunk *after* the underlying audio (and any VAD
    stop event) has already passed, so gating on VADUserStoppedSpeakingFrame
    ordering relative to TranscriptionFrame is unreliable — a debounced timer
    off the last transcript chunk is simpler and matches what's actually
    observable: every new chunk resets the wait; once it's quiet for
    TURN_GAP_SECS, the agent's turn is over.
    """

    def __init__(self, script: list[str]) -> None:
        super().__init__()
        self._script = script
        self._index = 0
        self._started = False
        self._pending_advance: asyncio.Task[None] | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, ClientConnectedFrame):
            await self.push_frame(frame, direction)
            if not self._started:
                self._started = True
                self._schedule_advance(JOIN_GRACE_SECS)
            return

        if isinstance(frame, TranscriptionFrame):
            logger.info(f"[reference agent said] {frame.text}")
            await self.push_frame(frame, direction)
            self._schedule_advance(TURN_GAP_SECS)
            return

        await self.push_frame(frame, direction)

    def _schedule_advance(self, delay: float) -> None:
        if self._pending_advance is not None:
            self._pending_advance.cancel()
        self._pending_advance = self.create_task(self._advance_after(delay))

    async def _advance_after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        await self._speak_next()

    async def _speak_next(self) -> None:
        if self._index >= len(self._script):
            logger.info("script complete, ending call")
            await self.push_frame(EndFrame())
            return
        line = self._script[self._index]
        self._index += 1
        self._agent_replied = False
        logger.info(f"[caller says] {line}")
        await self.push_frame(TTSSpeakFrame(text=line))


def _build_token(api_key: str, api_secret: str, room: str) -> str:
    grants = api.VideoGrants(room_join=True, room=room)
    return (
        api.AccessToken(api_key, api_secret)
        .with_identity("cadence-caller")
        .with_name("Cadence Caller")
        .with_grants(grants)
        .to_jwt()
    )


async def main() -> None:
    url = os.environ["LIVEKIT_URL"]
    api_key = os.environ["LIVEKIT_API_KEY"]
    api_secret = os.environ["LIVEKIT_API_SECRET"]
    creds_path = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]

    room_name = f"scripted-call-{uuid.uuid4().hex[:8]}"
    token = _build_token(api_key, api_secret, room_name)
    logger.info(f"joining room {room_name!r} as cadence-caller")

    transport = LiveKitTransport(
        url=url,
        token=token,
        room_name=room_name,
        params=LiveKitParams(audio_in_enabled=True, audio_out_enabled=True),
    )

    stt = GoogleSTTService(
        credentials_path=creds_path,
        settings=GoogleSTTService.Settings(enable_word_time_offsets=True),
    )
    tts = GoogleTTSService(
        credentials_path=creds_path,
        settings=GoogleTTSService.Settings(voice="en-US-Chirp3-HD-Charon"),
    )
    vad = VADProcessor(vad_analyzer=SileroVADAnalyzer())
    script_runner = ScriptRunner(SCRIPT)

    pipeline = Pipeline([transport.input(), vad, stt, script_runner, tts, transport.output()])
    worker = PipelineWorker(pipeline)
    runner = WorkerRunner()

    try:
        await asyncio.wait_for(runner.run(worker), timeout=RUN_TIMEOUT_SECS)
    except TimeoutError:
        logger.error("scripted call timed out")
        await worker.cancel()


if __name__ == "__main__":
    asyncio.run(main())
