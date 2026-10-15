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


def test_runtime_error_codes_are_declared_on_the_endpoints_that_raise_them() -> None:
    """#21: concurrency_limit (409) and batch_too_large (422) were real
    runtime behavior the contract never mentioned."""
    paths = json.loads(render_schema())["paths"]
    envelope = "#/components/schemas/ApiErrorResponse"

    def ref(path: str, code: str) -> str:
        return paths[path]["post"]["responses"][code]["content"]["application/json"]["schema"][
            "$ref"
        ]

    for path in ("/v1/simulations/runs", "/v1/discovery/runs"):
        assert ref(path, "409") == envelope
    assert ref("/v1/runs/{run_id}/rerun", "409") == envelope
    assert ref("/v1/runs/{run_id}/rerun", "422") == envelope
    assert ref("/v1/suites/{suite_id}/run", "422") == envelope
    # B4-04: red team is a batch now -- 422 (batch_too_large / identity_required), no 409.
    assert ref("/v1/redteam/runs", "422") == envelope
    assert "409" not in paths["/v1/redteam/runs"]["post"]["responses"]
    assert "409" not in paths["/v1/suites/{suite_id}/run"]["post"]["responses"]
