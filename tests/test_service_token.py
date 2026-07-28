from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.deps import require_user_id
from app.errors import register_exception_handlers
from app.middleware import ServiceTokenMiddleware

_TEST_TOKEN = "test-service-token"


def _build_test_app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(ServiceTokenMiddleware, token=_TEST_TOKEN)

    @app.get("/v1/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/dummy-writes")
    def create_dummy(user_id: str = Depends(require_user_id)) -> dict[str, str]:
        return {"createdBy": user_id}

    return app


def _client() -> TestClient:
    return TestClient(_build_test_app())


def _auth_headers(user_id: str | None = "user-123") -> dict[str, str]:
    headers = {"Authorization": f"Bearer {_TEST_TOKEN}"}
    if user_id is not None:
        headers["X-User-Id"] = user_id
    return headers


def test_healthz_exempt_from_token_check() -> None:
    response = _client().get("/v1/healthz")
    assert response.status_code == 200


def test_missing_token_is_401() -> None:
    response = _client().post("/v1/dummy-writes", json={})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_wrong_token_is_401() -> None:
    response = _client().post("/v1/dummy-writes", json={}, headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_correct_token_missing_user_id_on_write_route_is_400() -> None:
    response = _client().post("/v1/dummy-writes", json={}, headers=_auth_headers(user_id=None))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "missing_user_id"


def test_correct_token_and_user_id_succeeds() -> None:
    response = _client().post("/v1/dummy-writes", json={}, headers=_auth_headers())
    assert response.status_code == 200
    assert response.json() == {"createdBy": "user-123"}
