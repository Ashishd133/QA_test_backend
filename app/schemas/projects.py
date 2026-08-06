"""B2-06: GET /v1/projects response shape. No UI/switcher yet (ticket) --
this just proves the schema and endpoint exist ahead of B2-08 threading
project_id through run creation.
"""

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class APIModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class Project(APIModel):
    id: str
    name: str
