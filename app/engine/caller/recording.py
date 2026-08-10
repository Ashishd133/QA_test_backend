"""Optional room recording via LiveKit's Egress API, added so a run's
audio can be played back, not just its transcript read.

Audio-only room-composite egress, uploaded to a GCS bucket (reusing the
same service-account credentials as everything else -- see app.gcp_auth).
Entirely opt-in: if RECORDING_GCS_BUCKET isn't set, `start_room_recording`
returns None and the call proceeds exactly as before -- a missing bucket
should never be the reason a simulation run fails. The service account
also needs Storage Object Admin (or equivalent) on that bucket; Egress
will fail server-side otherwise, same no-op-on-failure handling applies
(see app/engine/caller/persona_call.py's use of this module).

The output filepath is a template we choose up front, so the final GCS URI
is knowable immediately once egress starts -- no need to poll egress
status or wait for a completion webhook before recording it on the run.
"""

from __future__ import annotations

import logging
import os

from livekit import api

from app.gcp_auth import google_credentials_json_string

logger = logging.getLogger(__name__)

_BUCKET_ENV_VAR = "RECORDING_GCS_BUCKET"


async def start_room_recording(
    *, url: str, api_key: str, api_secret: str, room_name: str
) -> tuple[str, str] | None:
    """Returns `(egress_id, gcs_uri)` if `RECORDING_GCS_BUCKET` is
    configured, else `None`. Never raises for a missing bucket (that's the
    expected no-op state until one is provisioned); does log+swallow any
    other egress-start failure, since a recording that didn't start is not
    a reason to fail the underlying test call."""
    bucket = os.environ.get(_BUCKET_ENV_VAR)
    if not bucket:
        return None

    filepath = f"recordings/{room_name}.ogg"
    try:
        async with api.LiveKitAPI(url, api_key, api_secret) as lkapi:
            info = await lkapi.egress.start_room_composite_egress(
                api.RoomCompositeEgressRequest(
                    room_name=room_name,
                    audio_only=True,
                    file_outputs=[
                        api.EncodedFileOutput(
                            file_type=api.EncodedFileType.OGG,
                            filepath=filepath,
                            gcp=api.GCPUpload(
                                credentials=google_credentials_json_string(), bucket=bucket
                            ),
                        )
                    ],
                )
            )
    except Exception:
        logger.exception(f"failed to start room recording for {room_name!r}, continuing without")
        return None
    return info.egress_id, f"gs://{bucket}/{filepath}"


async def stop_room_recording(*, url: str, api_key: str, api_secret: str, egress_id: str) -> None:
    """Best-effort: egress also stops on its own once the room empties, so
    a failure here just means the recording keeps going briefly longer
    (harmless) rather than the run failing over it."""
    try:
        async with api.LiveKitAPI(url, api_key, api_secret) as lkapi:
            await lkapi.egress.stop_egress(api.StopEgressRequest(egress_id=egress_id))
    except Exception:
        logger.exception(f"failed to stop egress {egress_id!r}")
