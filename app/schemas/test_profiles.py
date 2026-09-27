"""Request/response models for /v1/test-profiles (B2.7-03)."""

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class APIModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class TestProfileSummary(APIModel):
    """The list shape -- deliberately no `fields`. Decrypting and
    access-logging every row on a list call would be both slow and
    exactly the kind of blanket access "reads are access-logged" is meant
    to make visible, not routine; see TestProfileDetail for the one place
    plaintext is actually returned."""

    id: str
    project_id: str
    name: str
    encrypted: bool


class TestProfileDetail(APIModel):
    id: str
    project_id: str
    name: str
    # Always the decrypted plaintext here -- app/api/test_profiles.py
    # decrypts on every read and access-logs it; `encrypted` (below)
    # documents the storage guarantee, it's not a hint about this field's
    # own shape.
    fields: dict[str, object]
    encrypted: bool


class TestProfileCreate(APIModel):
    name: str
    fields: dict[str, object]


class TestProfileUpdate(APIModel):
    name: str | None = None
    fields: dict[str, object] | None = None
