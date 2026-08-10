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
import logging
import os
from datetime import timedelta

from google.auth.credentials import Credentials
from google.cloud.storage import Client as GCSClient  # type: ignore[import-untyped]
from google.oauth2 import service_account

logger = logging.getLogger(__name__)

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


def google_credentials_json_string() -> str:
    """The raw JSON key content, no matter which source is configured --
    for callers that always need JSON text regardless (LiveKit Egress's
    GCPUpload.credentials field, see app/engine/caller/recording.py)."""
    raw = os.environ.get(_JSON_ENV_VAR)
    if raw:
        return raw
    with open(os.environ[_PATH_ENV_VAR]) as f:
        return f.read()


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


_SIGNED_URL_TTL = timedelta(hours=1)


def signed_recording_url(gs_uri: str) -> str | None:
    """A time-limited, playable HTTPS URL for a `gs://bucket/path` URI
    (app.engine.caller.recording's output) -- generated fresh on every
    read (app/api/runs.py's get_run) rather than stored, so there's no
    stale-signature expiry to manage. Bucket stays private; nobody gets a
    URL without going through our own auth first. Returns None on any
    failure (bucket/object doesn't exist yet, wrong permissions, egress
    hasn't finished writing it) -- a run given the runaround like this
    shouldn't 500 the whole run detail response, and the object appearing
    later just means the URL is playable on a later request instead."""
    if not gs_uri.startswith("gs://"):
        return None
    bucket_name, _, blob_path = gs_uri.removeprefix("gs://").partition("/")
    if not bucket_name or not blob_path:
        return None
    try:
        client = GCSClient(credentials=load_google_oauth2_credentials())
        blob = client.bucket(bucket_name).blob(blob_path)
        return blob.generate_signed_url(version="v4", expiration=_SIGNED_URL_TTL, method="GET")  # type: ignore[no-any-return]
    except Exception:
        logger.exception(f"failed to sign recording URL for {gs_uri!r}")
        return None
