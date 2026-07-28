"""Forces the event payload models into the exported OpenAPI's
components.schemas, even though no route references them yet (spine §2:
"SSE event payload are Pydantic models included in the exported schema
... so stream types are generated too" — B0-07's SSE route has no JSON
response_model to hang these off of, so without this they'd never appear).
"""

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from app.schemas.events import (
    AssertionData,
    AttackData,
    DoneData,
    ErrorData,
    ExposureData,
    IntentData,
    MetricsData,
    NodeData,
    StatusData,
    TurnData,
)

EVENT_DATA_MODELS = (
    StatusData,
    TurnData,
    MetricsData,
    AssertionData,
    NodeData,
    IntentData,
    AttackData,
    ExposureData,
    DoneData,
    ErrorData,
)


def build_openapi_schema(app: FastAPI) -> dict[str, Any]:
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    schemas = schema.setdefault("components", {}).setdefault("schemas", {})
    for model in EVENT_DATA_MODELS:
        schemas[model.__name__] = model.model_json_schema()

    app.openapi_schema = schema
    return app.openapi_schema
