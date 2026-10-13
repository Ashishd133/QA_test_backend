"""B3-01: real pre-save handshake against a WebRTC (LiveKit) agent config.

Joins the room named by the agent's `room_url` + `token` exactly the way
the caller would, measures how long signaling + join took, and reports
what was negotiated. Never raises for a bad config -- every failure comes
back as a `ProbeResult` with one of three `error_code`s, because "your
config is wrong" is the successful outcome of a connection *test*:

- `bad_config`:        URL/token malformed before any network attempt.
- `auth_failed`:       server reached, but rejected or expired the token.
- `agent_unreachable`: timed out, DNS/TCP/TLS failure, or any other
                       connect failure that isn't clearly an auth one.

`rtt_ms` is the connect (signaling + join) wall time, not an ICE ping:
it's the number a user cares about for "is this agent reachable and
roughly how far away", and it needs no media to flow.
"""

import asyncio
import base64
import json
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal
from urllib.parse import urlparse

from livekit import rtc

from app.engine.livekit_creds import LiveKitCreds, mint_token, probe_room_name

ProbeErrorCode = Literal["agent_unreachable", "auth_failed", "bad_config"]

PROBE_TIMEOUT_SECS = 2.0

_AUTH_MARKERS = (
    "401",
    "403",
    "unauthorized",
    "forbidden",
    "invalid token",
    "permission",
    "expired",
)


@dataclass(frozen=True)
class ProbeResult:
    success: bool
    detail: str
    rtt_ms: int | None = None
    error_code: ProbeErrorCode | None = None


def _bad_config(detail: str) -> ProbeResult:
    return ProbeResult(False, detail, error_code="bad_config")


def _jwt_claims(token: str) -> dict[str, object] | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except ValueError:
        return None
    return claims if isinstance(claims, dict) else None


def validate_web_config(room_url: str, token: str) -> ProbeResult | None:
    """Returns a `bad_config` result, or None if the config is well-formed.
    Token expiry is an auth failure rather than a config-shape one, but is
    caught here too since it needs no network to know."""
    parsed = urlparse(room_url)
    if parsed.scheme not in ("ws", "wss", "http", "https") or not parsed.hostname:
        return _bad_config("room URL must be an absolute ws(s):// or http(s):// URL")
    claims = _jwt_claims(token)
    if claims is None:
        return _bad_config("token is not a valid JWT")
    exp = claims.get("exp")
    if isinstance(exp, (int, float)) and exp < time.time():
        return ProbeResult(False, "token has expired", error_code="auth_failed")
    return None


AGENT_JOIN_TIMEOUT_SECS = 8.0


def _agent_count(room: rtc.Room) -> int:
    return sum(
        1
        for p in room.remote_participants.values()
        if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
    )


async def probe_livekit(creds: LiveKitCreds, *, timeout: float = PROBE_TIMEOUT_SECS) -> ProbeResult:
    """B3-01b: probes with credentials (mints a short-lived token for a
    throwaway room) instead of a pasted JWT. If `creds.agent_name` is set,
    the token requests that agent's dispatch and the probe additionally
    waits for it to join -- a named agent that never shows up is a failed
    test, not a success, since a real call would be talking to nobody."""
    parsed = urlparse(creds.url)
    if parsed.scheme not in ("ws", "wss", "http", "https") or not parsed.hostname:
        return _bad_config("LiveKit URL must be an absolute ws(s):// or http(s):// URL")
    if not creds.api_key or not creds.api_secret:
        return _bad_config("API key and API secret are required")
    token = mint_token(creds, probe_room_name(), identity="cadence-probe", ttl=timedelta(minutes=5))
    return await probe_web_agent(
        creds.url,
        token,
        timeout=timeout,
        wait_for_agent=creds.agent_name,
    )


async def probe_web_agent(
    room_url: str,
    token: str,
    *,
    timeout: float = PROBE_TIMEOUT_SECS,
    wait_for_agent: str | None = None,
) -> ProbeResult:
    invalid = validate_web_config(room_url, token)
    if invalid is not None:
        return invalid

    room = rtc.Room()
    started = time.perf_counter()
    try:
        await asyncio.wait_for(room.connect(room_url, token), timeout=timeout)
    except TimeoutError:
        return ProbeResult(
            False,
            f"no response from {urlparse(room_url).hostname} within {timeout:g}s",
            error_code="agent_unreachable",
        )
    except rtc.ConnectError as exc:
        message = exc.message
        code: ProbeErrorCode = (
            "auth_failed"
            if any(marker in message.lower() for marker in _AUTH_MARKERS)
            else "agent_unreachable"
        )
        return ProbeResult(False, message, error_code=code)
    except Exception as exc:  # noqa: BLE001 -- a probe must report, never crash the request
        return ProbeResult(False, f"{type(exc).__name__}: {exc}", error_code="agent_unreachable")

    rtt_ms = round((time.perf_counter() - started) * 1000)
    try:
        if wait_for_agent:
            deadline = time.perf_counter() + AGENT_JOIN_TIMEOUT_SECS
            while _agent_count(room) == 0 and time.perf_counter() < deadline:
                await asyncio.sleep(0.1)
            if _agent_count(room) == 0:
                return ProbeResult(
                    False,
                    f"connected to LiveKit, but agent {wait_for_agent!r} did not join within "
                    f"{AGENT_JOIN_TIMEOUT_SECS:g}s -- check the agent name and that its worker "
                    "is running",
                    rtt_ms=rtt_ms,
                    error_code="agent_unreachable",
                )
        detail = f"LiveKit credentials verified (joined room {room.name!r}). " + (
            f"Agent {wait_for_agent!r} joined."
            if wait_for_agent
            else "No agent name set, so whether your agent answers calls isn't checked."
        )
    finally:
        await room.disconnect()
    return ProbeResult(True, detail, rtt_ms=rtt_ms)
