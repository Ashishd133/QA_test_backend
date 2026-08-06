"""B2-06: usage/cost tracking.

Threaded through the judge (app/engine/judge/judge.py -- every LLM call
records tokens) and exposed with the same interface for the caller runtime
to report STT/TTS usage once B2-08's executor wires it up. `as_dict()`'s
shape matches `runs.cost` exactly (see migrations/versions/
8b846bdde9ea_003_projects_cost_end_reason.py).

This is cost *visibility*, not billing (ticket: "no UI, no credit bar, no
project switcher, no billing -- schema and plumbing only") -- est_usd comes
from static per-unit rate constants, not a live pricing API.
"""

from __future__ import annotations

from dataclasses import dataclass

# Approximate, static USD rates -- Gemini 2.5 Flash and Google Cloud
# STT/TTS standard list pricing at time of writing. Not wired to any
# pricing API; revisit if rates change materially.
_LLM_INPUT_USD_PER_1K_TOKENS = 0.0003
_LLM_OUTPUT_USD_PER_1K_TOKENS = 0.0025
_STT_USD_PER_SECOND = 0.016 / 60  # ~$0.016/min, standard model
_TTS_USD_PER_1K_CHARS = 0.016  # Chirp 3 HD, ~$16/1M chars


@dataclass
class UsageTracker:
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    judge_calls: int = 0
    stt_seconds: float = 0.0
    tts_chars: int = 0

    def record_llm_call(self, *, input_tokens: int, output_tokens: int) -> None:
        self.judge_calls += 1
        self.llm_input_tokens += input_tokens
        self.llm_output_tokens += output_tokens

    def record_stt_seconds(self, seconds: float) -> None:
        self.stt_seconds += seconds

    def record_tts_chars(self, chars: int) -> None:
        self.tts_chars += chars

    @property
    def est_usd(self) -> float:
        return round(
            self.llm_input_tokens / 1000 * _LLM_INPUT_USD_PER_1K_TOKENS
            + self.llm_output_tokens / 1000 * _LLM_OUTPUT_USD_PER_1K_TOKENS
            + self.stt_seconds * _STT_USD_PER_SECOND
            + self.tts_chars / 1000 * _TTS_USD_PER_1K_CHARS,
            6,
        )

    def as_dict(self) -> dict[str, object]:
        """Matches `runs.cost`'s JSONB shape (camelCase, per the API's
        alias_generator=to_camel convention) exactly."""
        return {
            "llmInputTokens": self.llm_input_tokens,
            "llmOutputTokens": self.llm_output_tokens,
            "judgeCalls": self.judge_calls,
            "sttSeconds": round(self.stt_seconds, 3),
            "ttsChars": self.tts_chars,
            "estUsd": self.est_usd,
        }
