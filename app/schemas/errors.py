"""#21: the standard error envelope (`app.errors._envelope`) as a declared
schema. Every non-2xx body this API produces is `{"error": {...}}`; until
now only the success shapes were in the OpenAPI contract, so the real
`409 concurrency_limit` and `422 batch_too_large` were undocumented.

(`ErrorData` in app.schemas.events is a different thing: the SSE `error`
event's payload, not an HTTP error body.)
"""

from typing import Any

from pydantic import BaseModel


class ApiErrorBody(BaseModel):
    code: str
    message: str
    # Only present for some codes -- e.g. concurrency_limit carries
    # {"retryAfterMs": int}; validation_error carries a list of issues.
    details: Any | None = None


class ApiErrorResponse(BaseModel):
    error: ApiErrorBody


# `responses=` fragments for @router.post(...). Declared per endpoint, only
# for the codes that endpoint can actually raise.
CONCURRENCY_LIMIT_RESPONSE: dict[int | str, dict[str, Any]] = {
    409: {
        "model": ApiErrorResponse,
        "description": (
            "`concurrency_limit`: the agent already has `maxConcurrency` live "
            "runs. `error.details.retryAfterMs` is the suggested retry delay."
        ),
    }
}

# 422 on these endpoints is the error envelope for every code, not FastAPI's
# default HTTPValidationError -- see app.errors.validation_exception_handler.
BATCH_TOO_LARGE_RESPONSE: dict[int | str, dict[str, Any]] = {
    422: {
        "model": ApiErrorResponse,
        "description": (
            "`validation_error` (bad body, empty suite/batch), `not_supported` "
            "(personaIds/conditionProfileIds), or `batch_too_large` (the batch "
            "would queue more calls than `suite_run_batch_cap`)."
        ),
    }
}
