"""B2.5-01: /v1/projects CRUD + hard scoping.

Supersedes B2-06's read-only stub (GET /v1/projects returning the seeded
default project only) -- see app/api/projects.py's module docstring.
"""

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class APIModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class Project(APIModel):
    id: str
    name: str
    created_at: str


class ProjectCreate(APIModel):
    name: str


class ProjectUpdate(APIModel):
    name: str | None = None
