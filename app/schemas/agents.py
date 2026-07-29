"""Request/response models for /v1/agents (B1-02).

ConnectedAgent/AgentHealth field names match the frontend's src/types/
index.ts interfaces verbatim (same source as B1-01's Suite/Scenario). No
frontend type exists yet for agent *creation* (that form isn't built), so
AgentCreate/AgentUpdate/AgentDetail and the per-transport config shapes
below are this repo's own design -- see app/api/agents.py's module
docstring for the specific choices made and why.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

ConnectionTag = Literal["RTC", "SIP", "TEL"]
ConnectionStatus = Literal["Healthy", "Degraded", "Offline"]
ConnectionType = Literal["web", "sip", "phone"]


class APIModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ConnectedAgent(APIModel):
    id: str
    name: str
    meta: str
    tag: ConnectionTag
    status: ConnectionStatus


class AgentHealth(APIModel):
    id: str
    name: str
    meta: str
    uptime: str
    dot_color: str


class WebConfig(APIModel):
    transport: Literal["web"] = "web"
    room_url: str
    token: str


class SipConfig(APIModel):
    transport: Literal["sip"] = "sip"
    sip_uri: str
    username: str | None = None
    password: str | None = None


class PhoneConfig(APIModel):
    transport: Literal["phone"] = "phone"
    phone_number: str
    provider: str | None = None


AgentConfig = Annotated[WebConfig | SipConfig | PhoneConfig, Field(discriminator="transport")]


class AgentDetail(APIModel):
    id: str
    name: str
    transport: ConnectionType
    config: dict[str, object]
    language: str | None = None
    max_concurrency: int
    status: str | None = None
    last_seen_at: str | None = None


class AgentCreate(APIModel):
    name: str
    config: AgentConfig
    language: str | None = None
    max_concurrency: int = 1


class AgentUpdate(APIModel):
    name: str | None = None
    config: AgentConfig | None = None
    language: str | None = None
    max_concurrency: int | None = None


class TestConnectionResult(APIModel):
    success: bool
    rtt_ms: int | None = None
    detail: str
