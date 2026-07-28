from fastapi import FastAPI
from fastapi.routing import APIRoute

from app.api import healthz, runs
from app.config import get_settings
from app.errors import register_exception_handlers
from app.middleware import ServiceTokenMiddleware
from app.openapi import build_openapi_schema


def _stable_operation_id(route: APIRoute) -> str:
    return route.name


def create_app() -> FastAPI:
    app = FastAPI(
        title="cadence-brain",
        version="0.1.0",
        generate_unique_id_function=_stable_operation_id,
    )

    register_exception_handlers(app)
    app.add_middleware(ServiceTokenMiddleware, token=get_settings().python_service_token)
    app.include_router(healthz.router)
    app.include_router(runs.router)
    app.openapi = lambda: build_openapi_schema(app)  # type: ignore[method-assign]

    return app


app = create_app()
