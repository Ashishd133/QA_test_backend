"""B3-01b: per-agent LiveKit credentials and short-lived token minting.

An agent's `config` (jsonb) for a web transport is stored as
`{transport, roomUrl, apiKey, agentName?, secret: <crypto envelope>}` --
the API secret only ever exists encrypted at rest (app.crypto) and in
memory here. Legacy rows saved before B3-01b (`{roomUrl, token}`) and
agents with no stored credentials at all (seeded agents, the reference
agent) resolve to the server's own `LIVEKIT_*` env credentials, so
nothing that worked before stops working.
"""

import os
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from livekit import api

from app.crypto import decrypt_json, encrypt_json

_SECRET_KEY = "secret"


@dataclass(frozen=True)
class LiveKitCreds:
    url: str
    api_key: str
    api_secret: str
    agent_name: str | None = None

    @property
    def is_cadence_project(self) -> bool:
        """True iff these are the server's own LiveKit project. Recording
        (egress) is only safe there: elsewhere it would bill the user's
        project and write to Cadence's bucket."""
        own = os.environ.get("LIVEKIT_URL", "")
        return bool(own) and own.rstrip("/") == self.url.rstrip("/")


def env_creds() -> LiveKitCreds:
    return LiveKitCreds(
        url=os.environ["LIVEKIT_URL"],
        api_key=os.environ["LIVEKIT_API_KEY"],
        api_secret=os.environ["LIVEKIT_API_SECRET"],
    )


def build_stored_web_config(
    *, room_url: str, api_key: str, api_secret: str, agent_name: str | None
) -> dict[str, Any]:
    return {
        "transport": "web",
        "roomUrl": room_url,
        "apiKey": api_key,
        "agentName": agent_name,
        _SECRET_KEY: dict(encrypt_json({"apiSecret": api_secret})),
    }


def public_web_config(stored: dict[str, Any]) -> dict[str, Any]:
    """What API responses may show: everything except the secret."""
    return {
        "transport": "web",
        "roomUrl": stored.get("roomUrl"),
        "apiKey": stored.get("apiKey"),
        "agentName": stored.get("agentName"),
        "hasSecret": _SECRET_KEY in stored,
    }


def creds_from_stored(stored: dict[str, Any]) -> LiveKitCreds | None:
    """None for legacy/credential-less configs (caller falls back to env)."""
    if stored.get("transport") != "web" or _SECRET_KEY not in stored:
        return None
    secret = decrypt_json(stored[_SECRET_KEY])["apiSecret"]
    return LiveKitCreds(
        url=str(stored["roomUrl"]),
        api_key=str(stored["apiKey"]),
        api_secret=str(secret),
        agent_name=stored.get("agentName") or None,
    )


def stored_secret_envelope(stored: dict[str, Any]) -> dict[str, Any] | None:
    value = stored.get(_SECRET_KEY)
    return value if isinstance(value, dict) else None


def mint_token(
    creds: LiveKitCreds,
    room: str,
    *,
    identity: str = "cadence-caller",
    ttl: timedelta = timedelta(hours=1),
) -> str:
    token = (
        api.AccessToken(creds.api_key, creds.api_secret)
        .with_identity(identity)
        .with_name(identity)
        .with_grants(api.VideoGrants(room_join=True, room=room))
        .with_ttl(ttl)
    )
    if creds.agent_name:
        token = token.with_room_config(
            api.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name=creds.agent_name)])
        )
    return token.to_jwt()


def probe_room_name() -> str:
    return f"cadence-probe-{uuid.uuid4().hex[:8]}"
