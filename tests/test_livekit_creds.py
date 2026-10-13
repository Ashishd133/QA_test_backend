"""B3-01b: stored-config handling and token minting for bring-your-own LiveKit."""

import json

import jwt
import pytest

from app.engine.livekit_creds import (
    LiveKitCreds,
    build_stored_web_config,
    creds_from_stored,
    mint_token,
    public_web_config,
)

_SECRET = "s3cr3t-value-that-must-never-leak-0123456789"


def _stored(agent_name: str | None = None) -> dict[str, object]:
    return build_stored_web_config(
        room_url="wss://x.livekit.cloud",
        api_key="APIkey123",
        api_secret=_SECRET,
        agent_name=agent_name,
    )


def test_secret_is_encrypted_at_rest_and_hidden_in_public_view() -> None:
    stored = _stored()
    assert _SECRET not in json.dumps(stored)
    public = public_web_config(stored)
    assert _SECRET not in json.dumps(public)
    assert public["hasSecret"] is True
    assert public["apiKey"] == "APIkey123"


def test_stored_config_round_trips_to_creds() -> None:
    creds = creds_from_stored(_stored("my-agent"))
    assert creds == LiveKitCreds("wss://x.livekit.cloud", "APIkey123", _SECRET, "my-agent")


@pytest.mark.parametrize(
    "legacy",
    [
        {"transport": "web", "roomUrl": "wss://x", "token": "tok"},
        {"transport": "sip", "sipUri": "sip:a@b.com"},
        {},
    ],
)
def test_legacy_or_credentialless_configs_resolve_to_none(legacy: dict[str, object]) -> None:
    assert creds_from_stored(legacy) is None
    if legacy.get("transport") == "web":
        assert public_web_config(legacy)["hasSecret"] is False


def test_minted_token_is_signed_scoped_and_dispatches_named_agent() -> None:
    creds = LiveKitCreds("wss://x", "APIkey123", _SECRET, agent_name="my-agent")
    token = mint_token(creds, "room-1")
    claims = jwt.decode(token, _SECRET, algorithms=["HS256"], options={"verify_aud": False})
    assert claims["iss"] == "APIkey123"
    assert claims["video"]["room"] == "room-1"
    assert claims["roomConfig"]["agents"][0]["agentName"] == "my-agent"


def test_unnamed_agent_token_has_no_dispatch() -> None:
    token = mint_token(LiveKitCreds("wss://x", "k", _SECRET), "r")
    claims = jwt.decode(token, _SECRET, algorithms=["HS256"], options={"verify_aud": False})
    assert "roomConfig" not in claims


def test_is_cadence_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVEKIT_URL", "wss://own.livekit.cloud/")
    assert LiveKitCreds("wss://own.livekit.cloud", "k", "s").is_cadence_project
    assert not LiveKitCreds("wss://theirs.livekit.cloud", "k", "s").is_cadence_project
