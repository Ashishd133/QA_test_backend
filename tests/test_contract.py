import json

from app.openapi import EVENT_DATA_MODELS
from scripts.export_openapi import CONTRACT_PATH, render_schema


def test_export_is_deterministic() -> None:
    assert render_schema() == render_schema()


def test_event_payload_models_are_registered_as_components() -> None:
    schemas = json.loads(render_schema())["components"]["schemas"]
    for model in EVENT_DATA_MODELS:
        assert model.__name__ in schemas, f"{model.__name__} missing from OpenAPI components"


def test_committed_contract_matches_current_schema() -> None:
    assert CONTRACT_PATH.exists(), "contract/openapi.json is missing — run scripts.export_openapi"
    committed = CONTRACT_PATH.read_text()
    current = render_schema()
    assert committed == current, (
        "contract/openapi.json is stale — run `uv run python -m scripts.export_openapi` "
        "and commit the result"
    )
