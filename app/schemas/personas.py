"""Request/response models for /v1/personas (B2.7-02).

Supersedes app/schemas/dashboard.py's placeholder Persona (B1-06 -- never
consumed by any frontend type, so this replaces rather than adds
alongside it; pre-MVP has no deprecation window, per
Cadence_stitch_protocol.md §2).
"""

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class APIModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class PersonaDetail(APIModel):
    id: str
    # None = built-in (project-wide, read-only); a real value = a
    # user-authored persona scoped to that project, or a "duplicate to
    # project" copy of a built-in.
    project_id: str | None = None
    name: str
    voice: str
    language: str
    accent: str | None = None
    traits: dict[str, object]
    builtin: bool
    emotion: str | None = None
    speaking_rate: float | None = None
    interruption_behavior: str | None = None
    environment: str | None = None
    code_switch: dict[str, object] | None = None


class PersonaCreate(APIModel):
    name: str
    voice: str
    language: str
    accent: str | None = None
    traits: dict[str, object] = {}
    emotion: str | None = None
    speaking_rate: float | None = None
    interruption_behavior: str | None = None
    environment: str | None = None
    code_switch: dict[str, object] | None = None


class PersonaUpdate(APIModel):
    name: str | None = None
    voice: str | None = None
    language: str | None = None
    accent: str | None = None
    traits: dict[str, object] | None = None
    emotion: str | None = None
    speaking_rate: float | None = None
    interruption_behavior: str | None = None
    environment: str | None = None
    code_switch: dict[str, object] | None = None
