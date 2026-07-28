import secrets

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_EXEMPT_PATHS = {"/v1/healthz"}


def _unauthorized(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"error": {"code": "unauthorized", "message": message}},
    )


class ServiceTokenMiddleware:
    """Gates every request behind the shared Python service token.

    Python is never publicly routable (deploy-time guarantee), so a shared
    bearer token is sufficient here — see spine §0/§9. Parses X-User-Id /
    X-User-Email into request state for downstream dependencies (app.deps);
    it does NOT enforce X-User-Id itself, since not every route is a write.

    Pure ASGI, not BaseHTTPMiddleware: BaseHTTPMiddleware buffers the entire
    response body before forwarding it (a well-documented Starlette
    limitation), which silently breaks the SSE stream (B0-07) — headers
    arrive, but body bytes never flush until the response completes, and a
    live run never completes. A pure ASGI middleware passes `send` straight
    through to the inner app, so streaming responses stream.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self._token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)

        if request.url.path in _EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        auth_header = request.headers.get("authorization") or ""
        scheme, _, credential = auth_header.partition(" ")
        if scheme != "Bearer" or not secrets.compare_digest(credential, self._token):
            await _unauthorized("missing or invalid service token")(scope, receive, send)
            return

        scope.setdefault("state", {})
        scope["state"]["user_id"] = request.headers.get("x-user-id")
        scope["state"]["user_email"] = request.headers.get("x-user-email")

        await self.app(scope, receive, send)
