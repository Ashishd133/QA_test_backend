"""B2-04: persona-driven caller — Gemini Flash decides each line, unscripted.

Same transport/STT/TTS/VAD wiring as B2-02's `scripted_call.py`, but the
fixed `SCRIPT` list is replaced by `PersonaCaller`, which is asked for the
caller's next line after every agent turn given the transcript so far. The
turn budget cap is enforced here, not in `PersonaCaller`, as a hard safety
limit independent of the model's own `call_complete` judgment -- a
misbehaving reference agent (e.g. one that never resolves the ask) should
not be able to keep this running forever.

Run with `uv run python -m app.engine.caller.persona_call` while the
reference agent worker (`app.engine.reference_agent.agent dev`) is running
and registered with the same LiveKit project.
"""

from __future__ import annotations

import asyncio
import os
import uuid

from dotenv import load_dotenv
from google.oauth2 import service_account
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

from app.engine.caller.persona import PersonaCaller, PersonaSpec, Turn

load_dotenv()

# B2-04's own validation persona/scenario: "block my card" against the
# reference agent's actual verifiable identity (see
# app/engine/reference_agent/directory.py -- this is unrelated to the
# app/seed.py suite/persona fixtures, which the reference agent's directory
# knows nothing about).
CARD_BLOCK_PERSONA = PersonaSpec(
    name="Asha Rao",
    traits={"tone": "polite", "patience": "medium"},
    goal=(
        "You lost your debit card and need it blocked immediately. If asked "
        "to verify your identity, give: full name Asha Rao, date of birth "
        "April 12th 1990, security phrase 'blue lagoon'. Confirm the card is "
        "blocked and ask whether you'll get a replacement before ending the "
        "call."
    ),
    opening_line="Hi, I think I've lost my debit card and I need it blocked right away.",
)

JOIN_GRACE_SECS = 5.0  # let the target agent finish joining + connecting before we speak
TURN_GAP_SECS = 1.5  # pause after the agent stops talking before asking the persona LLM
MAX_TURNS = 10  # hard safety cap, independent of the model's own call_complete signal
RUN_TIMEOUT_SECS = 180.0


class PersonaRunner(FrameProcessor):
    """Drives the call turn-by-turn via `PersonaCaller`, in place of a fixed script.

    Debounces off `TranscriptionFrame`, same rationale as B2-02's
    `ScriptRunner`: STT finalizes a chunk after the underlying audio/VAD event
    has already passed, so a debounced timer off the transcript stream is the
    reliable turn-boundary signal, not frame ordering.
    """

    def __init__(self, persona_caller: PersonaCaller) -> None:
        super().__init__()
        self._persona_caller = persona_caller
        self._transcript: list[Turn] = []
        self._turns_taken = 0
        self._started = False
        self._done = False
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
            self._transcript.append(Turn(speaker="agent", text=frame.text))
            self._schedule_advance(TURN_GAP_SECS)
            return

        await self.push_frame(frame, direction)

    def _schedule_advance(self, delay: float) -> None:
        if self._done:
            return
        if self._pending_advance is not None:
            self._pending_advance.cancel()
        self._pending_advance = self.create_task(self._advance_after(delay))

    async def _advance_after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        await self._speak_next()

    async def _speak_next(self) -> None:
        if self._done:
            return
        if self._turns_taken >= MAX_TURNS:
            logger.warning(f"persona call hit MAX_TURNS={MAX_TURNS}, ending call")
            self._done = True
            await self.push_frame(EndFrame())
            return

        result = await self._persona_caller.next_turn(self._transcript)
        self._turns_taken += 1
        self._transcript.append(Turn(speaker="caller", text=result.utterance))
        logger.info(f"[caller says] {result.utterance}")
        await self.push_frame(TTSSpeakFrame(text=result.utterance))

        if result.call_complete:
            logger.info("persona LLM signalled call_complete, ending call")
            self._done = True
            await self.push_frame(EndFrame())


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
    project = os.environ["GOOGLE_CLOUD_PROJECT"]
    location = os.environ["GOOGLE_CLOUD_LOCATION"]

    room_name = f"persona-call-{uuid.uuid4().hex[:8]}"
    token = _build_token(api_key, api_secret, room_name)
    logger.info(f"joining room {room_name!r} as cadence-caller")

    credentials = service_account.Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
        creds_path, scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    persona_caller = PersonaCaller(
        CARD_BLOCK_PERSONA, credentials=credentials, project=project, location=location
    )

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
    persona_runner = PersonaRunner(persona_caller)

    pipeline = Pipeline([transport.input(), vad, stt, persona_runner, tts, transport.output()])
    worker = PipelineWorker(pipeline)
    runner = WorkerRunner()

    try:
        await asyncio.wait_for(runner.run(worker), timeout=RUN_TIMEOUT_SECS)
    except TimeoutError:
        logger.error("persona call timed out")
        await worker.cancel()


if __name__ == "__main__":
    asyncio.run(main())
