"""B2-03: latency clock — per-turn latency + barge-in counting at the transport layer.

Latency = caller utterance end (the caller's own TTS/output transport
finishing playout) -> target agent's first real audio frame back. Measured
via the first incoming raw audio frame whose energy clears a noise floor,
not a VAD start decision -- VAD's own `start_secs` smoothing (see
`VADProcessor`) would bias the number by its own detection lag, and the
spine is explicit that this must be a transport-layer measurement, not an
LLM-layer one.

Implemented as a Pipecat `BaseObserver` rather than a processor spliced into
the pipeline: an observer sees every frame pushed between every processor in
both directions, which is exactly what's needed here -- the caller's own
"stopped speaking" signal (`BotStoppedSpeakingFrame`, pushed by the output
transport) and the target's raw incoming audio (`UserAudioRawFrame`, pushed
by the input transport) sit on opposite sides of the pipeline and would
otherwise need two separate processors coordinating through shared state.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    UserAudioRawFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed

DEFAULT_THRESHOLD_MS = 1000
# int16 PCM RMS noise floor below which an incoming frame counts as silence,
# not the target agent's actual reply -- empirically tuned against the
# latency harness (mic/room noise sits well under this, real speech well over).
DEFAULT_SILENCE_RMS = 300.0


@dataclass
class TurnLatency:
    latency_ms: float
    flagged: bool
    flag_reason: str | None


class LatencyClock(BaseObserver):
    """Attach via `PipelineWorker(pipeline, observers=[clock])` on the caller's worker."""

    def __init__(
        self,
        *,
        threshold_ms: int = DEFAULT_THRESHOLD_MS,
        silence_rms: float = DEFAULT_SILENCE_RMS,
    ) -> None:
        super().__init__()
        self.threshold_ms = threshold_ms
        self.silence_rms = silence_rms
        self.turn_latencies: list[TurnLatency] = []
        self.interruption_count = 0

        self._caller_stopped_at_ns: int | None = None
        self._awaiting_agent_audio = False
        self._caller_speaking = False
        self._agent_speaking = False

    async def on_push_frame(self, data: FramePushed) -> None:
        frame = data.frame

        if isinstance(frame, BotStoppedSpeakingFrame):
            self._caller_speaking = False
            self._caller_stopped_at_ns = data.timestamp
            self._awaiting_agent_audio = True
            return

        if isinstance(frame, BotStartedSpeakingFrame):
            self._caller_speaking = True
            if self._agent_speaking:
                self.interruption_count += 1
            return

        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._agent_speaking = True
            if self._caller_speaking:
                self.interruption_count += 1
            return

        if isinstance(frame, VADUserStoppedSpeakingFrame):
            self._agent_speaking = False
            return

        if (
            isinstance(frame, UserAudioRawFrame)
            and self._awaiting_agent_audio
            and self._caller_stopped_at_ns is not None
            and _has_signal(frame.audio, self.silence_rms)
        ):
            latency_ms = (data.timestamp - self._caller_stopped_at_ns) / 1_000_000
            self._awaiting_agent_audio = False
            flagged = latency_ms > self.threshold_ms
            self.turn_latencies.append(
                TurnLatency(
                    latency_ms=latency_ms,
                    flagged=flagged,
                    flag_reason=(
                        f"latency {latency_ms:.0f}ms exceeded {self.threshold_ms}ms threshold"
                        if flagged
                        else None
                    ),
                )
            )


def _has_signal(pcm16: bytes, rms_threshold: float) -> bool:
    if not pcm16:
        return False
    samples = np.frombuffer(pcm16, dtype=np.int16).astype(np.float64)
    rms = float(np.sqrt(np.mean(samples**2)))
    return rms > rms_threshold
