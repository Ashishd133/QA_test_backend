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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

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

from app.engine.caller.latency_clock import LatencyClock, TurnLatency
from app.engine.caller.persona import PersonaCaller, PersonaSpec, Turn
from app.gcp_auth import google_credentials_kwargs, load_google_oauth2_credentials

# The narrow subset of runs.end_reason (B2-06) that a persona-driven call
# itself can determine: did the persona decide the call was done, did we
# have to force it via the turn/time safety cap, or did the executor ask us
# to stop early via `cancel_event`. "completed"/"agent_ended"/"error" are
# the B2-08 executor's own call, made with context this module doesn't have
# (whether scoring itself succeeded).
CallEndReason = Literal["caller_ended", "timeout", "cancelled"]

OnTurn = Callable[[Turn], Awaitable[None]]

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

    `on_turn`, if given, is awaited synchronously (in transcript order) right
    after each turn (caller or agent) is appended -- B2-08's executor uses
    this to `emit()` a `turn` event live, as the call happens, rather than
    only getting a full transcript back after the call ends (B2-04/B2-07's
    usage). It's a fast DB insert, not a model call, so awaiting it inline
    doesn't meaningfully stall the pipeline the way awaiting a judge call
    here would -- callers that need to react to a turn with something slow
    (e.g. B2-08's incremental judge) should fire that as their own
    `asyncio.create_task` from inside their `on_turn`, not block it.

    `cancel_event`, if given, is checked after every turn (both directions)
    -- once set, the call ends on its next check exactly like hitting
    MAX_TURNS, with `end_reason="cancelled"`. Checked after `on_turn`
    rather than before: the turn that was already in flight when
    cancellation was requested still gets recorded before the call stops,
    same as `fake_runner.py` finishing emitting an in-progress event before
    honoring a mid-script cancellation.
    """

    def __init__(
        self,
        persona_caller: PersonaCaller,
        *,
        on_turn: OnTurn | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> None:
        super().__init__()
        self._persona_caller = persona_caller
        self._on_turn = on_turn
        self._cancel_event = cancel_event
        self._transcript: list[Turn] = []
        self._turns_taken = 0
        self._started = False
        self._done = False
        self._pending_advance: asyncio.Task[None] | None = None
        self.end_reason: CallEndReason | None = None

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
            turn = Turn(speaker="agent", text=frame.text)
            self._transcript.append(turn)
            if self._on_turn is not None:
                await self._on_turn(turn)
            if await self._maybe_end_for_cancellation():
                return
            self._schedule_advance(TURN_GAP_SECS)
            return

        await self.push_frame(frame, direction)

    async def _maybe_end_for_cancellation(self) -> bool:
        if self._done or self._cancel_event is None or not self._cancel_event.is_set():
            return False
        logger.info("cancellation requested, ending call")
        self._done = True
        self.end_reason = "cancelled"
        await self.push_frame(EndFrame())
        return True

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
        if await self._maybe_end_for_cancellation():
            return
        if self._done:
            return
        if self._turns_taken >= MAX_TURNS:
            logger.warning(f"persona call hit MAX_TURNS={MAX_TURNS}, ending call")
            self._done = True
            self.end_reason = "timeout"
            await self.push_frame(EndFrame())
            return

        result = await self._persona_caller.next_turn(self._transcript)
        self._turns_taken += 1
        turn = Turn(speaker="caller", text=result.utterance)
        self._transcript.append(turn)
        logger.info(f"[caller says] {result.utterance}")
        if self._on_turn is not None:
            await self._on_turn(turn)
        if await self._maybe_end_for_cancellation():
            return
        await self.push_frame(TTSSpeakFrame(text=result.utterance))

        if result.call_complete:
            logger.info("persona LLM signalled call_complete, ending call")
            self._done = True
            self.end_reason = "caller_ended"
            await self.push_frame(EndFrame())

    @property
    def transcript(self) -> list[Turn]:
        return self._transcript


def _build_token(api_key: str, api_secret: str, room: str) -> str:
    grants = api.VideoGrants(room_join=True, room=room)
    return (
        api.AccessToken(api_key, api_secret)
        .with_identity("cadence-caller")
        .with_name("Cadence Caller")
        .with_grants(grants)
        .to_jwt()
    )


@dataclass(frozen=True)
class PersonaCallResult:
    transcript: list[Turn]
    # None only if the call never got far enough for PersonaRunner to reach
    # a natural end AND the outer RUN_TIMEOUT_SECS wrapper is what actually
    # cut it off -- treated the same as "timeout" by every caller, but kept
    # honest about which layer made the call rather than silently
    # defaulting one specific reason onto both.
    end_reason: CallEndReason
    turn_latencies: list[TurnLatency] = field(default_factory=list)
    interruption_count: int = 0


async def run_persona_call(
    persona: PersonaSpec,
    *,
    on_turn: OnTurn | None = None,
    latency_clock: LatencyClock | None = None,
    cancel_event: asyncio.Event | None = None,
    run_id: uuid.UUID | None = None,
) -> PersonaCallResult:
    """Runs one full call against whatever reference agent is currently
    registered with the LiveKit project (see this module's docstring), drives
    it with `persona`, and returns the resulting transcript plus why the call
    ended and its measured per-turn latencies. Extracted from the original
    single-persona `main()` (B2-04) so B2-07's `scripts/record_eval_transcripts.py`
    and B2-08's simulation executor can drive calls without duplicating the
    pipeline wiring.

    `on_turn`: see `PersonaRunner`'s docstring -- awaited live, in order, as
    each turn happens. Omit it (B2-04/B2-07's usage) to just get the full
    transcript back at the end.

    `latency_clock`: pass one in (rather than letting this construct its own
    private one) if the caller needs to read `clock.turn_latencies` live,
    during the call, from inside its own `on_turn` -- B2-08's executor does
    this to put real `latency_ms` on each `turn` event as it emits it,
    rather than only having latencies available after the whole call ends.

    `cancel_event`: checked by `PersonaRunner` after every turn -- B2-08's
    executor sets this from a concurrent poll of `runs.status` to stop a
    call early on user cancellation, the same "checked every turn" contract
    `fake_runner.py` implements for scripted runs (spine §5).

    `run_id`: B2-09's correlation key. When given: (1) every log line for
    the rest of this call, including ones deep inside PersonaRunner/
    PersonaCaller that only ever reference the bare module-level `logger`,
    gets `run_id` attached via `logger.contextualize` (a contextvar, not a
    logger instance threaded through -- see app/observability/logging.py);
    (2) pipecat's own auto-instrumented STT/TTS/LLM spans get `run_id` as a
    span attribute too, so a Phoenix trace waterfall for one run shows both
    the caller/STT spans and (via app/engine/executor/simulation.py's own
    spans, tagged with the same run_id) the judge spans, correlated.
    """
    url = os.environ["LIVEKIT_URL"]
    api_key = os.environ["LIVEKIT_API_KEY"]
    api_secret = os.environ["LIVEKIT_API_SECRET"]
    project = os.environ["GOOGLE_CLOUD_PROJECT"]
    location = os.environ["GOOGLE_CLOUD_LOCATION"]

    with logger.contextualize(run_id=str(run_id) if run_id is not None else None):
        room_name = f"persona-call-{uuid.uuid4().hex[:8]}"
        token = _build_token(api_key, api_secret, room_name)
        logger.info(f"joining room {room_name!r} as cadence-caller (persona={persona.name!r})")

        credentials = load_google_oauth2_credentials()
        persona_caller = PersonaCaller(
            persona, credentials=credentials, project=project, location=location
        )

        transport = LiveKitTransport(
            url=url,
            token=token,
            room_name=room_name,
            params=LiveKitParams(audio_in_enabled=True, audio_out_enabled=True),
        )

        google_credentials, google_credentials_path = google_credentials_kwargs()
        stt = GoogleSTTService(
            credentials=google_credentials,
            credentials_path=google_credentials_path,
            settings=GoogleSTTService.Settings(enable_word_time_offsets=True),
        )
        tts = GoogleTTSService(
            credentials=google_credentials,
            credentials_path=google_credentials_path,
            settings=GoogleTTSService.Settings(voice="en-US-Chirp3-HD-Charon"),
        )
        vad = VADProcessor(vad_analyzer=SileroVADAnalyzer())
        persona_runner = PersonaRunner(persona_caller, on_turn=on_turn, cancel_event=cancel_event)
        clock = latency_clock if latency_clock is not None else LatencyClock()

        pipeline = Pipeline([transport.input(), vad, stt, persona_runner, tts, transport.output()])
        worker = PipelineWorker(
            pipeline,
            observers=[clock],
            enable_tracing=run_id is not None,
            additional_span_attributes={"run_id": str(run_id)} if run_id is not None else None,
        )
        runner = WorkerRunner()

        try:
            await asyncio.wait_for(runner.run(worker), timeout=RUN_TIMEOUT_SECS)
        except TimeoutError:
            logger.error("persona call timed out")
            await worker.cancel()

        return PersonaCallResult(
            transcript=persona_runner.transcript,
            end_reason=persona_runner.end_reason or "timeout",
            turn_latencies=clock.turn_latencies,
            interruption_count=clock.interruption_count,
        )


async def main() -> None:
    await run_persona_call(CARD_BLOCK_PERSONA)


if __name__ == "__main__":
    asyncio.run(main())
