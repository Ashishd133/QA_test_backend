"""B3-01: probe_web_agent's error classification, with rtc.Room faked out."""

import asyncio
import base64
import json
import time
from collections.abc import Awaitable, Callable

import pytest
from livekit import rtc

from app.engine import agent_probe
from app.engine.agent_probe import probe_livekit, probe_web_agent, validate_web_config
from app.engine.livekit_creds import LiveKitCreds


def _jwt(**claims: object) -> str:
    def enc(obj: object) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{enc({'alg': 'HS256'})}.{enc(claims)}.sig"


_URL = "wss://example.livekit.cloud"
_TOKEN = _jwt(exp=int(time.time()) + 600)


class _FakeRoom:
    name = "demo"
    remote_participants: dict[str, object] = {}
    connect_impl: Callable[[], Awaitable[None]]

    async def connect(self, url: str, token: str) -> None:
        await type(self).connect_impl()

    async def disconnect(self) -> None:
        return None


def _use_fake(monkeypatch: pytest.MonkeyPatch, impl: Callable[[], Awaitable[None]]) -> None:
    _FakeRoom.connect_impl = staticmethod(impl)
    monkeypatch.setattr(rtc, "Room", _FakeRoom)


@pytest.mark.parametrize("url", ["", "not a url", "ftp://x.com", "wss://"])
async def test_bad_url_is_bad_config(url: str) -> None:
    result = await probe_web_agent(url, _TOKEN)
    assert (result.success, result.error_code) == (False, "bad_config")


async def test_non_jwt_token_is_bad_config() -> None:
    assert validate_web_config(_URL, "tok").error_code == "bad_config"  # type: ignore[union-attr]


async def test_expired_token_is_auth_failed() -> None:
    result = await probe_web_agent(_URL, _jwt(exp=1))
    assert result.error_code == "auth_failed"


async def test_success_reports_rtt(monkeypatch: pytest.MonkeyPatch) -> None:
    async def ok() -> None:
        await asyncio.sleep(0.01)

    _use_fake(monkeypatch, ok)
    result = await probe_web_agent(_URL, _TOKEN)
    assert result.success and result.error_code is None
    assert result.rtt_ms is not None and result.rtt_ms >= 10


async def test_timeout_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    async def hang() -> None:
        await asyncio.sleep(10)

    _use_fake(monkeypatch, hang)
    result = await probe_web_agent(_URL, _TOKEN, timeout=0.05)
    assert result.error_code == "agent_unreachable"


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("engine: signal failure: ws failure: HTTP error: 401 Unauthorized", "auth_failed"),
        ("invalid token: go-jose/go-jose: error in cryptographic primitive", "auth_failed"),
        ("could not dial: dns error: failed to lookup address", "agent_unreachable"),
    ],
)
async def test_connect_error_classification(
    monkeypatch: pytest.MonkeyPatch, message: str, code: str
) -> None:
    async def fail() -> None:
        raise rtc.ConnectError(message)

    _use_fake(monkeypatch, fail)
    result = await probe_web_agent(_URL, _TOKEN)
    assert result.error_code == code


async def test_probe_livekit_rejects_missing_credentials_and_bad_url() -> None:
    assert (await probe_livekit(LiveKitCreds(_URL, "", ""))).error_code == "bad_config"
    assert (await probe_livekit(LiveKitCreds("nope", "k", "s"))).error_code == "bad_config"


async def test_probe_livekit_mints_its_own_token_and_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def ok() -> None:
        return None

    _use_fake(monkeypatch, ok)
    result = await probe_livekit(LiveKitCreds(_URL, "key", "secret-value-0123456789abcdef0123"))
    assert result.success and result.error_code is None


async def test_probe_livekit_named_agent_that_never_joins_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def ok() -> None:
        return None

    _use_fake(monkeypatch, ok)
    monkeypatch.setattr(agent_probe, "AGENT_JOIN_TIMEOUT_SECS", 0.2)
    result = await probe_livekit(
        LiveKitCreds(_URL, "key", "secret-value-0123456789abcdef0123", agent_name="ghost")
    )
    assert result.success is False
    assert result.error_code == "agent_unreachable"
    assert "ghost" in result.detail
