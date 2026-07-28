from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.errors import APIError, register_exception_handlers


class _Payload(BaseModel):
    name: str


def _build_test_app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom/http")
    def raise_http() -> None:
        from fastapi import HTTPException

        raise HTTPException(status_code=409, detail="already exists")

    @app.get("/boom/api-error")
    def raise_api_error() -> None:
        raise APIError(code="unauthorized", message="missing token", status_code=401)

    @app.post("/boom/validate")
    def raise_validation(payload: _Payload) -> dict[str, str]:
        return {"name": payload.name}

    @app.get("/boom/unhandled")
    def raise_unhandled() -> None:
        raise RuntimeError("kaboom")

    return app


def _client() -> TestClient:
    return TestClient(_build_test_app(), raise_server_exceptions=False)


def test_http_exception_envelope() -> None:
    response = _client().get("/boom/http")
    assert response.status_code == 409
    assert response.json() == {"error": {"code": "conflict", "message": "already exists"}}


def test_api_error_envelope() -> None:
    response = _client().get("/boom/api-error")
    assert response.status_code == 401
    assert response.json() == {"error": {"code": "unauthorized", "message": "missing token"}}


def test_validation_error_envelope() -> None:
    response = _client().post("/boom/validate", json={})
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["message"] == "Request validation failed"
    assert isinstance(body["error"]["details"], list)
    assert body["error"]["details"][0]["loc"] == ["body", "name"]


def test_unknown_route_is_404_envelope() -> None:
    response = _client().get("/does-not-exist")
    assert response.status_code == 404
    assert response.json() == {"error": {"code": "not_found", "message": "Not Found"}}


def test_unhandled_exception_envelope() -> None:
    response = _client().get("/boom/unhandled")
    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "internal_error", "message": "An unexpected error occurred"}
    }
