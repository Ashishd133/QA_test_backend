"""Google Cloud TTS Chirp3-HD voice registry, used to validate a
persona's language/voice at write time (B2.7-02) instead of failing deep
inside a live simulation call.

Fetched live from Google's `ListVoices` API and cached for the process
lifetime: Google's own roster (currently ~1,568 Chirp3-HD voices across
~50 locales) changes rarely enough that a per-request live call is pure
latency for no freshness benefit, and hardcoding the list would drift the
moment Google adds or renames a voice.

The public functions are async and dispatch the (sync, gRPC) list_voices
call through `asyncio.to_thread` -- called directly from an async request
handler, it would block the whole event loop, SSE streams included, for
however long that one call takes. Once `_voice_registry` is cached the
`to_thread` hop is cheap (no real work happens in the thread), so this
costs nothing on the common path; it only matters for the first call after
process startup.

`accent` is deliberately NOT validated here: a Chirp3-HD `language_code`
(e.g. "en-IN") already fully encodes the regional accent Google actually
synthesizes -- validating both would mean inventing a second
accent-to-locale mapping that duplicates what the language code already
says. `accent` stays a free-text, display-only label, same as the
existing seeded personas (app/seed.py), which predate any such mapping and
use bare, non-BCP-47 `language` values ("en") -- those rows are grandfathered
(read-only built-ins; this registry only gates new writes) rather than
retrofitted.
"""

from __future__ import annotations

import asyncio
from functools import lru_cache

from google.cloud import texttospeech

from app.gcp_auth import load_google_oauth2_credentials

_CHIRP3_MARKER = "Chirp3-HD"


@lru_cache(maxsize=1)
def _voice_registry() -> dict[str, frozenset[str]]:
    client = texttospeech.TextToSpeechClient(credentials=load_google_oauth2_credentials())
    registry: dict[str, set[str]] = {}
    for voice in client.list_voices().voices:
        if _CHIRP3_MARKER not in voice.name:
            continue
        for language_code in voice.language_codes:
            registry.setdefault(language_code, set()).add(voice.name)
    return {lang: frozenset(names) for lang, names in registry.items()}


async def supported_languages() -> list[str]:
    registry = await asyncio.to_thread(_voice_registry)
    return sorted(registry)


async def supported_voices(language: str) -> list[str]:
    registry = await asyncio.to_thread(_voice_registry)
    return sorted(registry.get(language, ()))


async def is_supported(*, language: str, voice: str) -> bool:
    registry = await asyncio.to_thread(_voice_registry)
    return voice in registry.get(language, frozenset())
