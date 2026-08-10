"""Shared Google service-account credential loading for the caller runtime
(app/engine/caller/persona_call.py) and the judge client
(app/engine/executor/simulation.py).

Two sources, checked in this order:

  1. GOOGLE_APPLICATION_CREDENTIALS_JSON -- the service account key's raw
     JSON content itself, as an env var. Railway (and most container
     platforms) have no clean way to mount an arbitrary file into a
     container, but setting an env var is trivial -- this is the standard
     workaround, and it's also exactly the format pipecat's own
     GoogleSTTService/GoogleTTSService already accept via their
     `credentials=` kwarg (a JSON string, not a path -- see
     pipecat/services/google/stt.py), so no re-encoding is needed to use
     both call shapes from the same source.
  2. GOOGLE_APPLICATION_CREDENTIALS -- a file path to the key (local dev:
     the actual JSON key file sits on disk next to the repo, .gitignore'd
     so it's never in the image -- which is exactly why Railway needs #1).

Diagnosed 2026-08: every simulation run on Railway was failing instantly
with `judge_client_failed` because GOOGLE_APPLICATION_CREDENTIALS pointed
at a path that only ever existed on developer machines -- the deployed
container had no such file. Not a network issue, a deployment one.
"""

from __future__ import annotations

import json
import os

from google.auth.credentials import Credentials
from google.oauth2 import service_account

_CLOUD_PLATFORM_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
_JSON_ENV_VAR = "GOOGLE_APPLICATION_CREDENTIALS_JSON"
_PATH_ENV_VAR = "GOOGLE_APPLICATION_CREDENTIALS"


def google_credentials_kwargs() -> tuple[str | None, str | None]:
    """`(credentials, credentials_path)` for GoogleSTTService/
    GoogleTTSService (pipecat) -- exactly one is non-None, matching
    whichever credential source is configured. Returned as a tuple rather
    than a dict to splat: mypy can't verify a `**dict[str, str]` splat
    against those constructors' precisely-typed keyword params, and both
    are legitimately `str | None` kwargs there already (pipecat's own code
    checks `if credentials: ... elif credentials_path: ...`), so passing
    both straight through -- one real, one None -- is exactly what it
    expects."""
    raw = os.environ.get(_JSON_ENV_VAR)
    if raw:
        return raw, None
    return None, os.environ[_PATH_ENV_VAR]


def load_google_oauth2_credentials() -> Credentials:
    """The `google.auth.credentials.Credentials` object PersonaCaller/
    build_vertex_client need (genai.Client(credentials=...))."""
    raw = os.environ.get(_JSON_ENV_VAR)
    if raw:
        info = json.loads(raw)
        return service_account.Credentials.from_service_account_info(  # type: ignore[no-untyped-call,no-any-return]
            info, scopes=_CLOUD_PLATFORM_SCOPES
        )
    creds_path = os.environ[_PATH_ENV_VAR]
    return service_account.Credentials.from_service_account_file(  # type: ignore[no-untyped-call,no-any-return]
        creds_path, scopes=_CLOUD_PLATFORM_SCOPES
    )
