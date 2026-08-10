"""B2-09: structured logging setup.

Reuses loguru rather than introducing a second logging framework -- it's
already the caller module's own logger (`app/engine/caller/persona_call.py`
and friends import it directly), pulled in transitively via pipecat-ai.
`configure_logging()` should be called once at process startup (worker
main / reference agent entrypoint); everything downstream just keeps using
`from loguru import logger` as before.

Run-scoped correlation (the ticket's "keyed by run_id") is layered on top
via `logger.contextualize(run_id=...)` at the top of a run, not by passing
a bound logger through every function -- loguru's contextualize uses a
contextvar, so every `logger.info(...)` call anywhere in that async call
stack (including deep inside PersonaRunner, which only ever references the
bare module-level `logger`) picks up run_id automatically, with no signature
changes needed at each call site. See app/engine/executor/simulation.py and
app/engine/caller/persona_call.py's `run_persona_call` for the two places
that open a contextualize block.
"""

from __future__ import annotations

import sys

from loguru import logger

_JSON_FORMAT_ENV_VAR = "LOG_JSON"


def configure_logging(*, json: bool | None = None) -> None:
    """`json=None` (the default) reads the LOG_JSON env var so this can be
    toggled per-environment (Railway: LOG_JSON=1 for greppable/structured
    production logs; local dev: unset, for loguru's readable colorized
    default) without a code change between them."""
    import os

    use_json = json if json is not None else os.environ.get(_JSON_FORMAT_ENV_VAR, "") == "1"

    logger.remove()
    logger.add(sys.stderr, serialize=use_json, level="INFO", backtrace=False, diagnose=False)
