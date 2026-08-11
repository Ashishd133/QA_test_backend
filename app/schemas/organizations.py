"""B2.5-02: GET /v1/organizations.

One response per caller, nested projects + agent counts included -- the
switcher popover is one request, never N+1 (stitch protocol's named
reverse-dependency for this ticket, `B2.5-02`'s nested-projects response).
"""

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class APIModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class OrganizationProject(APIModel):
    id: str
    name: str
    agent_count: int


class Organization(APIModel):
    id: str
    name: str
    slug: str
    projects: list[OrganizationProject]
