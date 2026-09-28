"""B2-08: the real simulation executor -- wires B2-01 through B2-07 into one
live pipeline. Mirrors `fake_runner.py`'s lifecycle shape (claim already
happened; set status=running; heartbeat; per-turn cancellation check;
single completion transaction with materialize+metrics) but drives an
actual persona-vs-reference-agent call instead of replaying a script:

  claim (already done by caller) -> status=running
    -> run_persona_call() against the scenario's persona/goal
         -> on_turn: emit(turn) live, with best-effort correlated latency
         -> after each agent turn: incremental judge (fire-and-forget,
            at most one in flight) -> emit(assertion/metrics)
         -> cancel_event set by a concurrent runs.status poll
    -> final judge over the whole transcript -> emit(assertion/done)
    -> materialize_run + one UPDATE (status/ended_at/metrics/end_reason/cost)

Run with `python -m app.workers.main` (registers this via
`app.workers.executors.EXECUTORS["simulation"]`) while
`app.engine.reference_agent.agent dev` is running and registered with the
same LiveKit project.

B2-09: every log line for a run (including ones inside run_persona_call/
PersonaRunner that this module never calls directly) carries `run_id` via
`logger.contextualize` -- see app/observability/logging.py. Spans: one root
`run.simulation` span per run, `judge.incremental_evaluate`/
`judge.final_evaluate` child spans around the two judge calls, plus
whatever pipecat auto-instruments for the caller/STT/TTS pipeline inside
`run_persona_call` (enabled by passing `run_id` through to it) -- all
sharing the run_id attribute, all under the one TracerProvider
`app.observability.tracing.setup_tracing()` configures at worker startup.

B2.6-05: every span this module creates also carries `parent_run_id`
(`_span_attributes`, `ClaimedRun.parent_run_id` from B3-03's claim query)
when this run is a batch child -- omitted for a standalone Test Run.
Filtering Phoenix by one `parent_run_id` returns spans from every call in
that batch.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Literal

from loguru import logger
from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import get_settings
from app.engine.caller.latency_clock import LatencyClock
from app.engine.caller.persona import PersonaSpec, Turn
from app.engine.caller.persona_call import CallEndReason, run_persona_call
from app.engine.judge.judge import FinalJudge, GenAIClient, IncrementalJudge, build_vertex_client
from app.engine.judge.models import AssertionSpec
from app.engine.metrics.compiler import (
    MetricOutcome,
    compile_metrics,
    evaluate_builtin_metric,
    evaluate_python_metric,
    not_sampled_outcomes,
    outcomes_from_judge_verdicts,
)
from app.engine.metrics.resolver import resolve_metrics_for_agent
from app.events import (
    assertion_event,
    done_event,
    emit,
    error_event,
    metric_result_event,
    metrics_event,
    status_event,
    turn_event,
)
from app.gcp_auth import load_google_oauth2_credentials
from app.observability.tracing import get_tracer
from app.schemas.runs import TranscriptTurn
from app.usage import UsageTracker
from app.workers.claim import ClaimedRun
from app.workers.fake_runner import run_fake_script
from app.workers.heartbeat import heartbeat_loop
from app.workers.materialize import materialize_run
from app.workers.rollup import maybe_close_parent


def _span_attributes(
    run_id: uuid.UUID, parent_run_id: uuid.UUID | None, **extra: str
) -> dict[str, str]:
    """B2.6-05: `parent_run_id` on every span this executor creates (not
    just the root `run.simulation` one), so a single Phoenix attribute
    filter finds a whole batch's waterfall without relying on trace-tree
    expansion from a root span. Omitted (not emitted as empty/None) for a
    standalone Test Run -- ClaimedRun.parent_run_id is None there."""
    attrs = {"run_id": str(run_id), **extra}
    if parent_run_id is not None:
        attrs["parent_run_id"] = str(parent_run_id)
    return attrs


# Same bounded-shutdown discipline as fake_runner.py's heartbeat teardown
# (test-suite-resource-exhaustion / B1-08's cousin: an uncancellable wedge
# in one task must never hang the whole run).
_HEARTBEAT_SHUTDOWN_TIMEOUT_SECONDS = 5.0
_CANCEL_WATCHER_SHUTDOWN_TIMEOUT_SECONDS = 5.0
# Coarser than the heartbeat's own 10s tick (app/workers/heartbeat.py) --
# cancellation responsiveness within a few seconds is plenty for a human
# clicking "cancel", and this is one extra query per tick against the same
# `runs` row the heartbeat and reaper already touch.
_CANCEL_POLL_INTERVAL_SECONDS = 3.0

_SCENARIO_SQL = text(
    "SELECT sc.persona, sc.script, sc.assertions, sc.goal AS scenario_goal, "
    "       p.voice AS persona_voice, p.language AS persona_language, "
    "       p.accent AS persona_accent, p.emotion AS persona_emotion, "
    "       p.speaking_rate AS persona_speaking_rate, p.traits AS persona_traits "
    "FROM scenarios sc LEFT JOIN personas p ON p.id = sc.persona_id "
    "WHERE sc.id = :id"
)


def _load_assertion_specs(raw: object) -> list[AssertionSpec]:
    assert isinstance(raw, list)
    specs = []
    for a in raw:
        assert isinstance(a, dict)
        specs.append(
            AssertionSpec(
                id=a["id"],
                name=a["name"],
                # Most scenarios (app/seed.py's dashboard filler) only carry
                # bare {id, name} -- B2-05's own documented gap. Falling
                # back to `name` keeps those runnable (if someone points a
                # real run at one) instead of crashing on a missing field;
                # scenario:reference:card-block always has a real one.
                description=a.get("description") or a["name"],
                distinguish_from=a.get("distinguishFrom", ""),
            )
        )
    return specs


def _build_persona_spec(persona_name: str, script: object, persona_row: RowMapping) -> PersonaSpec:
    """`persona_row` carries the columns `_SCENARIO_SQL`'s LEFT JOIN adds --
    all None when the scenario has no linked `persona_id` (the common case
    today; see PersonaSpec's own docstring for why every field here stays
    optional rather than becoming a hard requirement)."""
    script_dict: dict[str, object] = script if isinstance(script, dict) else {}

    # Persona-row traits first (the authoritative, editable set once B2.7-02
    # ships a UI for it), script-level traits second so scenario-specific
    # authoring can still override a specific key -- in practice these two
    # sources don't currently overlap for any seeded scenario (either a
    # scenario has script content or a linked persona_id, not both), but
    # there's no reason to make that an assumption the merge order depends on.
    persona_row_traits = persona_row["persona_traits"]
    traits: dict[str, object] = dict(persona_row_traits) if persona_row_traits else {}
    script_traits = script_dict.get("traits")
    if isinstance(script_traits, dict):
        traits.update(script_traits)
    if persona_row["persona_accent"]:
        traits.setdefault("accent", persona_row["persona_accent"])
    if persona_row["persona_emotion"]:
        traits.setdefault("emotion", persona_row["persona_emotion"])

    return PersonaSpec(
        name=persona_name,
        traits=traits,
        goal=str(
            script_dict.get("goal")
            or (
                f"You are {persona_name}, calling Cadence Bank customer support. "
                "Explain what you need naturally, cooperate with any identity "
                "verification, and end the call once your concern is addressed."
            )
        ),
        opening_line=str(script_dict.get("openingLine") or "Hi, I need some help with my account."),
        voice=persona_row["persona_voice"],
        language=persona_row["persona_language"],
        speaking_rate=persona_row["persona_speaking_rate"],
    )


async def _load_scenario(
    engine: AsyncEngine, scenario_id: uuid.UUID
) -> tuple[PersonaSpec, list[AssertionSpec], bool, str | None]:
    """Third element: whether the scenario carried a real `script` (as
    opposed to `_build_persona_spec`'s generic fallback content) -- see
    `run_simulation`'s own use of it. Most scenarios (app/seed.py's
    dashboard filler, e.g. "Card & Account Support") have none; only
    scenarios meant to run against a real, live-registered agent do.

    Fourth element: `scenarios.goal` -- B2.7-09's judged success criterion,
    named `scenario_goal` everywhere downstream and never just `goal`,
    because `PersonaSpec.goal` (built by `_build_persona_spec` from
    `script.goal`) is a completely different concept: the synthetic
    caller's own instruction for what to say/ask for, not what the final
    judge evaluates. Conflating the two variable names is exactly how this
    ticket's own two tiers would get silently merged back into one."""
    async with engine.connect() as conn:
        row = (await conn.execute(_SCENARIO_SQL, {"id": scenario_id})).mappings().first()
    if row is None:
        raise ValueError(f"scenario {scenario_id} not found")
    has_real_script = isinstance(row["script"], dict) and bool(row["script"])
    return (
        _build_persona_spec(row["persona"], row["script"], row),
        _load_assertion_specs(row["assertions"]),
        has_real_script,
        row["scenario_goal"],
    )


async def _resolve_run_metrics(engine: AsyncEngine, agent_id: uuid.UUID) -> list[RowMapping]:
    """B2.7-06: resolved once per run (not once per metric), scoped to the
    agent's project. `resolve_metrics_for_agent` already applies the
    agent > project > builtin precedence chain (app.engine.metrics.
    resolver) -- this is just the project_id lookup that call needs,
    since ClaimedRun carries agent_id but not project_id directly."""
    async with engine.connect() as conn:
        project_id = (
            await conn.execute(
                text("SELECT project_id FROM agents WHERE id = :id"), {"id": agent_id}
            )
        ).scalar_one()
        return await resolve_metrics_for_agent(conn, project_id=project_id, agent_id=agent_id)


def _build_judge_client() -> GenAIClient:
    settings = get_settings()
    credentials = load_google_oauth2_credentials()
    client = build_vertex_client(
        credentials=credentials,
        project=settings.google_cloud_project,
        location=settings.google_cloud_location,
    )
    return client  # type: ignore[return-value]


async def _poll_for_cancellation(
    engine: AsyncEngine, run_id: uuid.UUID, cancel_event: asyncio.Event
) -> None:
    """Runs until it sees status='cancelled' (spine §5: checked regularly,
    same contract fake_runner.py implements by polling between script
    entries) or is cancelled itself by the executor on normal completion."""
    while True:
        await asyncio.sleep(_CANCEL_POLL_INTERVAL_SECONDS)
        async with engine.connect() as conn:
            status = (
                await conn.execute(text("SELECT status FROM runs WHERE id = :id"), {"id": run_id})
            ).scalar_one_or_none()
        if status == "cancelled":
            cancel_event.set()
            return


def _end_reason_for(call_end_reason: CallEndReason, was_cancelled: bool) -> str:
    """Maps PersonaRunner's narrow signal onto the full runs.end_reason
    enum -- the executor's own knowledge (was the run actually cancelled
    via the API, independent of whether PersonaRunner's own cancel_event
    check happened to fire first) takes priority."""
    if was_cancelled:
        return "cancelled"
    if call_end_reason == "timeout":
        return "timeout"
    return "completed"


async def _shutdown_task(task: asyncio.Task[None], *, timeout: float, label: str) -> None:
    """Cancel a background task and bound how long we wait for it to
    actually finish -- same discipline as the heartbeat teardown everywhere
    else in this codebase (test-suite-resource-exhaustion's lesson: an
    uncancellable wedge in one task must never hang the caller)."""
    task.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except asyncio.CancelledError:
        pass
    except TimeoutError:
        logger.warning(f"{label} task did not shut down within {timeout}s of cancellation")


async def run_simulation(engine: AsyncEngine, claimed: ClaimedRun) -> None:
    """Public entrypoint: just opens the run_id logging context (B2-09) and
    delegates. Kept separate from `_run_simulation_body` rather than
    threading `with logger.contextualize(...)` through the existing nested
    try/finally structure below -- contextualize is a contextvar, so
    wrapping the outer call is equivalent and doesn't force re-indenting
    the whole function."""
    with logger.contextualize(run_id=str(claimed.id)):
        await _run_simulation_body(engine, claimed)


async def _run_simulation_body(engine: AsyncEngine, claimed: ClaimedRun) -> None:
    run_id = claimed.id
    if claimed.scenario_id is None:
        # Every simulation run is created from a scenario (app/api/runs.py's
        # create_simulation_run) -- a null scenario_id here means the row
        # was created some other way and this executor can't run it.
        async with engine.connect() as conn, conn.begin():
            await emit(
                conn,
                run_id,
                error_event(
                    code="missing_scenario", message="simulation run has no scenario_id", fatal=True
                ),
            )
            await conn.execute(
                text(
                    "UPDATE runs SET status = 'failed', ended_at = now(), "
                    "end_reason = 'error' WHERE id = :id"
                ),
                {"id": run_id},
            )
            await maybe_close_parent(conn, run_id)
        return

    try:
        persona_spec, assertion_specs, has_real_script, scenario_goal = await _load_scenario(
            engine, claimed.scenario_id
        )
    except Exception:
        logger.exception(f"failed to load scenario for run {run_id}")
        async with engine.connect() as conn, conn.begin():
            await emit(
                conn,
                run_id,
                error_event(
                    code="scenario_load_failed", message="could not load scenario", fatal=True
                ),
            )
            await conn.execute(
                text(
                    "UPDATE runs SET status = 'failed', ended_at = now(), "
                    "end_reason = 'error' WHERE id = :id"
                ),
                {"id": run_id},
            )
            await maybe_close_parent(conn, run_id)
        return

    if not has_real_script:
        # Dashboard-filler scenarios (app/seed.py: "Card & Account Support",
        # "Billing & Payments") carry no real script/persona content and
        # have no live agent worker behind them -- only
        # scenario:reference:card-block (and anything else deliberately
        # built for this executor) does. Routing those through a real
        # persona-vs-agent call used to mean a ~180s hang waiting for an
        # agent that will never join, then a low/zero score, instead of the
        # instant scripted replay they're supposed to give the dashboard.
        # FakeRunner owns status transitions from 'claimed' itself, so this
        # must happen before this function's own status='running' write.
        logger.info(f"scenario for run {run_id} has no real script -- delegating to FakeRunner")
        await run_fake_script(engine, claimed)
        return

    try:
        resolved_metrics = await _resolve_run_metrics(engine, claimed.agent_id)
    except Exception:
        # A metric can never take the whole run down (B2.7-04's own "never
        # a crashed executor" bar extends to resolving them, not just
        # scoring them) -- fall back to an empty set and keep going;
        # assertions/goal still score normally either way.
        logger.exception(f"failed to resolve metrics for run {run_id}, scoring none")
        resolved_metrics = []

    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text("UPDATE runs SET status = 'running', started_at = now() WHERE id = :id"),
            {"id": run_id},
        )
        await emit(conn, run_id, status_event(status="running"))

    heartbeat_task = asyncio.create_task(heartbeat_loop(engine, run_id))
    cancel_event = asyncio.Event()
    cancel_watcher_task = asyncio.create_task(_poll_for_cancellation(engine, run_id, cancel_event))

    with get_tracer().start_as_current_span(
        "run.simulation",
        attributes=_span_attributes(
            run_id, claimed.parent_run_id, scenario_id=str(claimed.scenario_id)
        ),
    ):
        await _run_simulation_traced(
            engine,
            run_id,
            claimed.parent_run_id,
            persona_spec,
            assertion_specs,
            resolved_metrics,
            scenario_goal,
            cancel_event,
            cancel_watcher_task,
            heartbeat_task,
        )


async def _run_simulation_traced(
    engine: AsyncEngine,
    run_id: uuid.UUID,
    parent_run_id: uuid.UUID | None,
    persona_spec: PersonaSpec,
    assertion_specs: list[AssertionSpec],
    resolved_metrics: list[RowMapping],
    scenario_goal: str | None,
    cancel_event: asyncio.Event,
    cancel_watcher_task: asyncio.Task[None],
    heartbeat_task: asyncio.Task[None],
) -> None:
    # From here on, heartbeat_task and cancel_watcher_task are live -- every
    # exit path (including the early `return`s below, and any exception
    # from a step we don't explicitly guard) must go through the `finally`
    # at the bottom of this function so neither task leaks in the worker
    # process. That's why the rest of the body is one big `try`, rather
    # than several independent ones each responsible for its own cleanup.
    try:
        try:
            usage = UsageTracker()
            client = _build_judge_client()
        except Exception:
            logger.exception(f"failed to build judge client for run {run_id}")
            async with engine.connect() as conn, conn.begin():
                await emit(
                    conn,
                    run_id,
                    error_event(
                        code="judge_client_failed",
                        message="could not initialize judge client",
                        fatal=True,
                    ),
                )
                await conn.execute(
                    text(
                        "UPDATE runs SET status = 'failed', ended_at = now(), "
                        "end_reason = 'error' WHERE id = :id"
                    ),
                    {"id": run_id},
                )
                await maybe_close_parent(conn, run_id)
            return

        incremental_judge = IncrementalJudge(client, usage=usage)
        final_judge = FinalJudge(client, usage=usage)

        transcript: list[Turn] = []
        assertion_statuses: dict[str, str] = {a.id: "undetermined" for a in assertion_specs}
        latency_clock = LatencyClock()
        turn_index = 0
        agent_turns_seen = 0
        pending_incremental: asyncio.Task[None] | None = None

        async def run_incremental() -> None:
            try:
                judge_transcript = [TranscriptTurn(role=t.speaker, text=t.text) for t in transcript]
                with get_tracer().start_as_current_span(
                    "judge.incremental_evaluate",
                    attributes=_span_attributes(run_id, parent_run_id),
                ):
                    verdict = await incremental_judge.evaluate(
                        assertion_specs, judge_transcript, dict(assertion_statuses)
                    )
            except Exception:
                # "cheap, temperature 0" per the spine, but still a real
                # network call -- a transient failure here shouldn't take
                # the run down; the final judge gets the last word regardless.
                logger.exception(f"incremental judge failed for run {run_id}, continuing live")
                return

            async with engine.connect() as conn, conn.begin():
                for flip in verdict.flips:
                    assertion_statuses[flip.assertion_id] = flip.status
                    name = next(
                        (a.name for a in assertion_specs if a.id == flip.assertion_id),
                        flip.assertion_id,
                    )
                    await emit(
                        conn,
                        run_id,
                        assertion_event(
                            assertion_id=flip.assertion_id,
                            name=name,
                            status=flip.status,
                            triggered_at_turn=flip.turn_refs[0] if flip.turn_refs else None,
                            note=flip.rationale,
                        ),
                    )
                await emit(conn, run_id, metrics_event(score=verdict.live_score / 100))

        async def on_turn(turn: Turn) -> None:
            nonlocal turn_index, agent_turns_seen, pending_incremental
            transcript.append(turn)

            if turn.speaker == "caller":
                # The persona's line is synthesized to audio by the
                # caller's own TTS to send into the room -- count it here
                # rather than leaving ttsChars permanently at 0 (sttSeconds
                # stays 0: the reference agent's turns arrive as audio the
                # caller-side pipeline STTs, but no duration is plumbed
                # through Turn to attribute yet).
                usage.record_tts_chars(len(turn.text))

            latency_ms: int | None = None
            if turn.speaker == "agent":
                # Best-effort correlation, not a guarantee: LatencyClock
                # appends a TurnLatency the moment enough incoming agent
                # audio signal arrives, which happens before STT finishes
                # transcribing and this turn's TranscriptionFrame fires --
                # so the Nth agent turn should line up with the Nth
                # recorded latency. If the pipeline ever produces a
                # TranscriptionFrame without a preceding detected-audio
                # event (e.g. the very first agent turn racing the
                # observer's own setup), this just leaves latency_ms unset
                # rather than misattributing a different turn's number.
                if agent_turns_seen < len(latency_clock.turn_latencies):
                    latency_ms = round(latency_clock.turn_latencies[agent_turns_seen].latency_ms)
                agent_turns_seen += 1

            async with engine.connect() as conn, conn.begin():
                await emit(
                    conn,
                    run_id,
                    turn_event(
                        index=turn_index, role=turn.speaker, text=turn.text, latency_ms=latency_ms
                    ),
                )
            turn_index += 1

            if turn.speaker == "agent" and (
                pending_incremental is None or pending_incremental.done()
            ):
                pending_incremental = asyncio.create_task(run_incremental())

        try:
            result = await run_persona_call(
                persona_spec,
                on_turn=on_turn,
                latency_clock=latency_clock,
                cancel_event=cancel_event,
                run_id=run_id,
            )
        except Exception:
            logger.exception(f"persona call failed for run {run_id}")
            async with engine.connect() as conn, conn.begin():
                await emit(
                    conn,
                    run_id,
                    error_event(
                        code="call_failed", message="the simulated call failed", fatal=True
                    ),
                )
                await conn.execute(
                    text(
                        "UPDATE runs SET status = 'failed', ended_at = now(), "
                        "end_reason = 'error' WHERE id = :id"
                    ),
                    {"id": run_id},
                )
                await maybe_close_parent(conn, run_id)
            return
        finally:
            await _shutdown_task(
                cancel_watcher_task,
                timeout=_CANCEL_WATCHER_SHUTDOWN_TIMEOUT_SECONDS,
                label=f"cancel watcher (run {run_id})",
            )

        # A stray in-flight incremental check from the last agent turn: let
        # it finish so its assertion/metrics events land before the final
        # pass writes its own (final wins on conflict either way --
        # materialize_run's upsert is last-write-wins by seq order -- but
        # not racing them avoids a confusing double-flip in the live event
        # stream for no reason).
        if pending_incremental is not None and not pending_incremental.done():
            try:
                await asyncio.wait_for(asyncio.shield(pending_incremental), timeout=30.0)
            except (asyncio.CancelledError, TimeoutError):
                logger.warning(
                    f"final incremental judge pass for run {run_id} didn't finish in time"
                )

        was_cancelled = cancel_event.is_set()
        final_usd_score: float | None = None
        end_reason = _end_reason_for(result.end_reason, was_cancelled)

        try:
            if was_cancelled:
                # Cancellation wins outright: don't score a call the user
                # asked to stop, even if it happened to reach a natural
                # end_reason at the same moment (spine §5's partial-done
                # contract, same as fake_runner.py's cancellation branch).
                partial = done_event(score=None, result_badge=None)
                async with engine.connect() as conn, conn.begin():
                    current_status = (
                        await conn.execute(
                            text("SELECT status FROM runs WHERE id = :id FOR UPDATE"),
                            {"id": run_id},
                        )
                    ).scalar_one()
                    if current_status not in ("claimed", "running", "cancelled"):
                        return  # reaper already resurrected this run as failed
                    await emit(conn, run_id, partial)
                    await materialize_run(conn, run_id)
                    metrics = {
                        **partial.data.model_dump(mode="json", by_alias=True),
                        "avgLatencyMs": _avg_latency_ms(latency_clock),
                        "interruptions": latency_clock.interruption_count,
                    }
                    await conn.execute(
                        text(
                            "UPDATE runs SET status = 'cancelled', ended_at = now(), "
                            "metrics = CAST(:metrics AS jsonb), end_reason = :end_reason, "
                            "cost = CAST(:cost AS jsonb), recording_url = :recording_url "
                            "WHERE id = :id"
                        ),
                        {
                            "id": run_id,
                            "metrics": json.dumps(metrics),
                            "end_reason": end_reason,
                            "cost": json.dumps(usage.as_dict()),
                            "recording_url": result.recording_url,
                        },
                    )
                    await maybe_close_parent(conn, run_id)
                return

            judge_transcript = [TranscriptTurn(role=t.speaker, text=t.text) for t in transcript]
            compiled = compile_metrics(resolved_metrics, run_id=run_id)

            with get_tracer().start_as_current_span(
                "judge.final_evaluate", attributes=_span_attributes(run_id, parent_run_id)
            ):
                final_verdict = await final_judge.evaluate(
                    assertion_specs,
                    judge_transcript,
                    metrics=compiled.llm_judge_signals,
                    goal=scenario_goal,
                )
            final_usd_score = final_verdict.final_score / 100

            # B2.7-06: builtin/python metrics dispatch outside the judge
            # call entirely (they don't touch the LLM at all, so they
            # never count against the "≤2 calls per turn" budget); python
            # ones run concurrently since each is an independent sandboxed
            # subprocess with its own timeout.
            metric_outcomes: list[MetricOutcome] = outcomes_from_judge_verdicts(
                final_verdict.metrics, compiled.llm_judge_rows
            )
            latencies_ms = [tl.latency_ms for tl in latency_clock.turn_latencies]
            metric_outcomes += [
                evaluate_builtin_metric(row, judge_transcript, latencies_ms)
                for row in compiled.builtin_rows
            ]
            if compiled.python_rows:
                python_context: dict[str, object] = {
                    "transcript": [{"role": t.role, "text": t.text} for t in judge_transcript]
                }
                metric_outcomes += await asyncio.gather(
                    *(evaluate_python_metric(row, python_context) for row in compiled.python_rows)
                )
            metric_outcomes += not_sampled_outcomes(compiled.not_sampled_rows)
            metric_names = {str(row["id"]): row["name"] for row in resolved_metrics}

            async with engine.connect() as conn, conn.begin():
                current_status = (
                    await conn.execute(
                        text("SELECT status FROM runs WHERE id = :id FOR UPDATE"), {"id": run_id}
                    )
                ).scalar_one()
                if current_status not in ("claimed", "running"):
                    # Reaper-resurrection guard (fake_runner.py's same
                    # check): it already marked this run failed/worker_lost
                    # while the call was in flight -- must not emit a
                    # second, contradictory terminal frame on top of that.
                    return

                for note in final_verdict.assertions:
                    await emit(
                        conn,
                        run_id,
                        assertion_event(
                            assertion_id=note.assertion_id,
                            name=next(
                                (a.name for a in assertion_specs if a.id == note.assertion_id),
                                note.assertion_id,
                            ),
                            status=note.status,
                            triggered_at_turn=note.turn_refs[0] if note.turn_refs else None,
                            note=note.note,
                        ),
                    )

                for outcome in metric_outcomes:
                    await emit(
                        conn,
                        run_id,
                        metric_result_event(
                            metric_id=outcome.metric_id,
                            metric_version=outcome.metric_version,
                            name=metric_names.get(outcome.metric_id, outcome.metric_id),
                            status=outcome.status,
                            value=outcome.value
                            if isinstance(outcome.value, str | float | bool)
                            else None,
                            turn_refs=outcome.turn_refs,
                            rationale=outcome.rationale,
                        ),
                    )

                result_badge = _badge_from_final_score(final_verdict.final_score)
                done = done_event(score=final_usd_score, result_badge=result_badge)
                await emit(conn, run_id, done)
                await materialize_run(conn, run_id)

                metrics = {
                    **done.data.model_dump(mode="json", by_alias=True),
                    "avgLatencyMs": _avg_latency_ms(latency_clock),
                    "turnsCompleted": len(transcript),
                    "interruptions": latency_clock.interruption_count,
                    "sentiment": final_verdict.sentiment,
                    "summary": final_verdict.summary,
                    # B2.7-09: None/[] when the scenario had no goal to
                    # evaluate (final_judge.evaluate's `goal` kwarg was
                    # None) -- app/api/runs.py reads these straight off
                    # this same runs.metrics jsonb, same place score/
                    # sentiment/summary already live.
                    "goalMet": final_verdict.goal_met,
                    "goalAnalysis": final_verdict.goal_analysis,
                    "goalTurnRefs": final_verdict.goal_turn_refs,
                }
                await conn.execute(
                    text(
                        "UPDATE runs SET status = 'completed', ended_at = now(), "
                        "metrics = CAST(:metrics AS jsonb), end_reason = :end_reason, "
                        "cost = CAST(:cost AS jsonb), recording_url = :recording_url "
                        "WHERE id = :id"
                    ),
                    {
                        "id": run_id,
                        "metrics": json.dumps(metrics),
                        "end_reason": end_reason,
                        "cost": json.dumps(usage.as_dict()),
                        "recording_url": result.recording_url,
                    },
                )
                await maybe_close_parent(conn, run_id)
        except Exception:
            logger.exception(f"final scoring/materialization failed for run {run_id}")
            async with engine.connect() as conn, conn.begin():
                await emit(
                    conn,
                    run_id,
                    error_event(code="scoring_failed", message="final scoring failed", fatal=True),
                )
                await conn.execute(
                    text(
                        "UPDATE runs SET status = 'failed', ended_at = now(), "
                        "end_reason = 'error', cost = CAST(:cost AS jsonb) WHERE id = :id"
                    ),
                    {"id": run_id, "cost": json.dumps(usage.as_dict())},
                )
                await maybe_close_parent(conn, run_id)
    finally:
        await _shutdown_task(
            heartbeat_task,
            timeout=_HEARTBEAT_SHUTDOWN_TIMEOUT_SECONDS,
            label=f"heartbeat (run {run_id})",
        )


def _avg_latency_ms(clock: LatencyClock) -> float | None:
    if not clock.turn_latencies:
        return None
    return sum(t.latency_ms for t in clock.turn_latencies) / len(clock.turn_latencies)


def _badge_from_final_score(final_score: int) -> Literal["pass", "warn", "fail"]:
    """Same thresholds as app.verdict.badge_from_score, kept as its own
    function here rather than reused: that one returns the broader
    `Verdict` (which also has "idle", for in-progress runs) and takes a 0-1
    score, whereas `done_event`'s `result_badge` needs the narrower 3-value
    Literal and `final_verdict.final_score` is already 0-100 -- reusing it
    would mean converting the scale and then re-narrowing the return type
    right back down, for no actual benefit over just having two thresholds
    in two places."""
    if final_score >= 80:
        return "pass"
    if final_score >= 50:
        return "warn"
    return "fail"
