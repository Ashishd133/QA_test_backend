"""B2-09: OTel tracing setup -- one process-wide TracerProvider shared by
two span sources:

  - pipecat's own auto-instrumented STT/TTS/LLM spans, turned on per-call
    via `PipelineWorker(enable_tracing=True, additional_span_attributes=...)`
    (see app/engine/caller/persona_call.py's `run_persona_call`)
  - the executor's own explicit spans (a root span per run, judge-call
    spans) via `get_tracer().start_as_current_span(...)`
    (see app/engine/executor/simulation.py)

`setup_tracing()` should be called once at worker process startup
(app/workers/main.py). It's idempotent -- safe to call more than once.

Verification: Phoenix (https://github.com/Arize-ai/phoenix) speaks OTLP
natively. Run it locally with `uv run --with arize-phoenix phoenix serve`
(no Docker needed) and set OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
before starting the worker to see the trace waterfall -- it's a one-off
viewing tool, not a project dependency, so it's deliberately not added to
pyproject.toml. With no endpoint configured, spans go to the console
instead so tracing is still visible (if noisy) without Phoenix running.
"""

from __future__ import annotations

import logging
import os

from opentelemetry import trace
from opentelemetry.trace import Tracer

logger = logging.getLogger(__name__)

_SERVICE_NAME = "cadence-worker"
_OTLP_ENDPOINT_ENV_VAR = "OTEL_EXPORTER_OTLP_ENDPOINT"
_tracing_configured = False


def setup_tracing() -> bool:
    global _tracing_configured
    if _tracing_configured:
        return True

    # Imported lazily (rather than at module load) so importing this module
    # never fails just because pipecat/opentelemetry aren't installed in
    # some other context -- setup_tracing() itself already degrades to a
    # logged warning + no-op spans if that's the case, same as pipecat's
    # own is_tracing_available() escape hatch.
    from pipecat.utils.tracing.setup import setup_tracing as _pipecat_setup_tracing

    endpoint = os.environ.get(_OTLP_ENDPOINT_ENV_VAR, "")
    exporter = None
    if endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)

    ok = _pipecat_setup_tracing(
        service_name=_SERVICE_NAME, exporter=exporter, console_export=not endpoint
    )
    if not ok:
        logger.warning("OTel tracing setup failed or unavailable -- spans will be no-ops")
    _tracing_configured = ok
    return ok


def get_tracer() -> Tracer:
    return trace.get_tracer(_SERVICE_NAME)
