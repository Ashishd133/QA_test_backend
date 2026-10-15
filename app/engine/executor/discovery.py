"""B5-03: the real discovery executor.

Same lifecycle shape as `simulation.py` (status=running, heartbeat, cancel
watcher, reaper-resurrection guard, one completion transaction), but the
caller is the B5-02 `ExplorerCaller` and the output is a map, not a score:

  claim -> resolve dummy identity (test_profiles, access-logged)
    -> propose candidate topics -> status=running
    -> run_persona_call(planner=ExplorerCaller)
         -> on_turn: emit(turn)
         -> on each observation: emit(node) for new/changed nodes, and
            emit(intent) as intents are reached      (map grows live)
    -> graph.finalize(): a still-unresolved gate becomes blocked
    -> emit(final node/intent frames, done{discoveryResult})
    -> materialize turns + discovery_nodes/edges/intents from the event log

Drafts are *not* generated here: B2.7-12's `scenarios:generate
source=discovery_run` turns this run's `discovery_intents` into drafts, which
is the B5-03 amendment's intended path (one generation machinery, one
acceptance path).

A wrong-value identity is not an error: the run completes with the gate
`blocked` and a human-readable reason. A run whose agent never produced a
single observation (never answered) fails instead of "completing" an empty
map.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from typing import Protocol

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.api.test_profiles import resolve_dummy_identity
from app.config import get_settings
from app.engine.caller.latency_clock import LatencyClock
from app.engine.caller.persona import PersonaSpec, Turn
from app.engine.caller.persona_call import OnTurn, PersonaCallResult, TurnPlanner, run_persona_call
from app.engine.discovery.explorer import (
    DEFAULT_TURN_BUDGET,
    OPENING_LINE,
    ExplorerCaller,
    ExplorerLLM,
    GeminiExplorerLLM,
    propose_candidates,
)
from app.engine.discovery.graph import FlowGraph, IntentSummary, ObserveResult
from app.engine.discovery.models import TurnObservation
from app.engine.executor.simulation import (
    _CANCEL_WATCHER_SHUTDOWN_TIMEOUT_SECONDS,
    _HEARTBEAT_SHUTDOWN_TIMEOUT_SECONDS,
    _avg_latency_ms,
    _end_reason_for,
    _load_livekit_creds,
    _poll_for_cancellation,
    _shutdown_task,
)
from app.engine.livekit_creds import LiveKitCreds
from app.events import (
    done_event,
    emit,
    error_event,
    intent_event,
    node_event,
    status_event,
    turn_event,
)
from app.gcp_auth import load_google_oauth2_credentials
from app.schemas.events import DiscoveryResult
from app.usage import UsageTracker
from app.workers.claim import ClaimedRun
from app.workers.heartbeat import heartbeat_loop
from app.workers.materialize import materialize_discovery, materialize_run
from app.workers.rollup import maybe_close_parent

# 40 caller turns at roughly 10-15s each, plus join/teardown headroom.
DISCOVERY_RUN_TIMEOUT_SECS = 900.0

_RUN_SQL = text(
    "SELECT r.config, r.created_by_user_id, a.name, a.description, a.prompt "
    "FROM runs r JOIN agents a ON a.id = r.agent_id WHERE r.id = :id"
)

_EXPLORER_PERSONA = PersonaSpec(
    name="Cadence Explorer",
    traits={},
    goal="Map the agent's call flow.",
    opening_line=OPENING_LINE,
)


class CallRunner(Protocol):
    async def __call__(
        self,
        persona: PersonaSpec,
        *,
        on_turn: OnTurn | None,
        latency_clock: LatencyClock | None,
        cancel_event: asyncio.Event | None,
        run_id: uuid.UUID | None,
        livekit: LiveKitCreds | None,
        planner: TurnPlanner | None,
        max_turns: int,
        run_timeout_secs: float,
    ) -> PersonaCallResult: ...


def _default_llm() -> ExplorerLLM:
    settings = get_settings()
    return GeminiExplorerLLM(
        credentials=load_google_oauth2_credentials(),
        project=settings.google_cloud_project,
        location=settings.google_cloud_location,
    )


async def _fail(engine: AsyncEngine, run_id: uuid.UUID, code: str, message: str) -> None:
    async with engine.connect() as conn, conn.begin():
        await emit(conn, run_id, error_event(code=code, message=message, fatal=True))
        await conn.execute(
            text(
                "UPDATE runs SET status = 'failed', ended_at = now(), "
                "end_reason = 'error' WHERE id = :id"
            ),
            {"id": run_id},
        )
        await maybe_close_parent(conn, run_id)


async def run_discovery(
    engine: AsyncEngine,
    claimed: ClaimedRun,
    *,
    run_call: CallRunner = run_persona_call,
    make_llm: Callable[[], ExplorerLLM] = _default_llm,
) -> None:
    with logger.contextualize(run_id=str(claimed.id)):
        await _run_discovery_body(engine, claimed, run_call=run_call, make_llm=make_llm)


async def _emit_node_frames(
    conn: AsyncConnection, run_id: uuid.UUID, graph: FlowGraph, keys: list[str]
) -> None:
    by_key = {n.key: n for n in graph.nodes}
    for key in keys:
        node = by_key[key]
        await emit(
            conn,
            run_id,
            node_event(
                node_id=node.key,
                label=node.label,
                x=node.x,
                y=node.y,
                state=node.state,
                blocked_reason=node.blocked_reason,
                from_nodes=list(node.incoming),
            ),
        )


async def _emit_intent_frames(
    conn: AsyncConnection, run_id: uuid.UUID, intents: list[IntentSummary]
) -> None:
    for intent in intents:
        await emit(
            conn,
            run_id,
            intent_event(
                name=intent.name, state=intent.state, path=intent.path, reason=intent.reason
            ),
        )


async def _run_discovery_body(
    engine: AsyncEngine,
    claimed: ClaimedRun,
    *,
    run_call: CallRunner,
    make_llm: Callable[[], ExplorerLLM],
) -> None:
    run_id = claimed.id

    try:
        async with engine.connect() as conn:
            row = (await conn.execute(_RUN_SQL, {"id": run_id})).mappings().one()
        identity = await resolve_dummy_identity(
            engine, claimed.config, user_id=row["created_by_user_id"]
        )
    except Exception:
        logger.exception(f"failed to load discovery run {run_id}")
        await _fail(engine, run_id, "discovery_load_failed", "could not load this discovery run")
        return
    if identity is None:
        await _fail(engine, run_id, "missing_identity", "discovery run has no dummy identity")
        return

    try:
        livekit_creds = await _load_livekit_creds(engine, claimed.agent_id)
    except Exception:
        # Never log the config itself -- it holds an encrypted secret (see
        # simulation.py's identical guard).
        logger.exception(f"failed to load LiveKit credentials for run {run_id}")
        await _fail(
            engine,
            run_id,
            "agent_credentials_unavailable",
            "could not load this agent's LiveKit credentials",
        )
        return

    try:
        llm = make_llm()
    except Exception:
        logger.exception(f"failed to build explorer client for run {run_id}")
        await _fail(engine, run_id, "explorer_init_failed", "could not initialize the explorer")
        return

    candidates = await propose_candidates(
        llm, name=row["name"], description=row["description"], prompt=row["prompt"]
    )

    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text("UPDATE runs SET status = 'running', started_at = now() WHERE id = :id"),
            {"id": run_id},
        )
        await emit(conn, run_id, status_event(status="running"))

    heartbeat_task = asyncio.create_task(heartbeat_loop(engine, run_id))
    cancel_event = asyncio.Event()
    cancel_watcher_task = asyncio.create_task(_poll_for_cancellation(engine, run_id, cancel_event))

    usage = UsageTracker()
    latency_clock = LatencyClock()
    graph = FlowGraph()
    turn_index = 0
    agent_turns_seen = 0

    async def on_observation(index: int, obs: TurnObservation, result: ObserveResult) -> None:
        if not result.changed:
            return
        changed_labels = {n.label for n in graph.nodes if n.key in result.changed}
        async with engine.connect() as conn, conn.begin():
            await _emit_node_frames(conn, run_id, graph, result.changed)
            await _emit_intent_frames(
                conn, run_id, [i for i in graph.intents() if i.name in changed_labels]
            )

    explorer = ExplorerCaller(
        llm,
        identity=identity,
        candidates=candidates,
        graph=graph,
        turn_budget=DEFAULT_TURN_BUDGET,
        on_observation=on_observation,
    )

    async def on_turn(turn: Turn) -> None:
        nonlocal turn_index, agent_turns_seen
        latency_ms: int | None = None
        if turn.speaker == "caller":
            usage.record_tts_chars(len(turn.text))
        else:
            # Same best-effort correlation as simulation.py's on_turn.
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

    try:
        try:
            result = await run_call(
                _EXPLORER_PERSONA,
                on_turn=on_turn,
                latency_clock=latency_clock,
                cancel_event=cancel_event,
                run_id=run_id,
                livekit=livekit_creds,
                planner=explorer,
                max_turns=DEFAULT_TURN_BUDGET,
                run_timeout_secs=DISCOVERY_RUN_TIMEOUT_SECS,
            )
        except Exception:
            logger.exception(f"discovery call failed for run {run_id}")
            await _fail(engine, run_id, "call_failed", "the discovery call failed")
            return
        finally:
            await _shutdown_task(
                cancel_watcher_task,
                timeout=_CANCEL_WATCHER_SHUTDOWN_TIMEOUT_SECONDS,
                label=f"cancel watcher (run {run_id})",
            )

        was_cancelled = cancel_event.is_set()
        end_reason = _end_reason_for(result.end_reason, was_cancelled)

        try:
            if not graph.nodes and not was_cancelled:
                await _fail(
                    engine, run_id, "no_observations", "the agent never responded to the explorer"
                )
                return

            changed_at_end = graph.finalize()
            intents = graph.intents(explorer.candidates)
            async with engine.connect() as conn, conn.begin():
                current_status = (
                    await conn.execute(
                        text("SELECT status FROM runs WHERE id = :id FOR UPDATE"), {"id": run_id}
                    )
                ).scalar_one()
                if current_status not in ("claimed", "running", "cancelled"):
                    return  # reaper already resurrected this run as failed

                if was_cancelled:
                    done = done_event(score=None, result_badge=None)
                    final_status = "cancelled"
                else:
                    await _emit_node_frames(conn, run_id, graph, changed_at_end)
                    await _emit_intent_frames(conn, run_id, intents)
                    summary = _summarize(graph, intents, explorer)
                    done = done_event(discovery_result=summary)
                    final_status = "completed"

                await emit(conn, run_id, done)
                await materialize_run(conn, run_id)
                await materialize_discovery(conn, run_id)
                metrics = {
                    **done.data.model_dump(mode="json", by_alias=True),
                    "avgLatencyMs": _avg_latency_ms(latency_clock),
                    "turnsCompleted": turn_index,
                    "interruptions": latency_clock.interruption_count,
                }
                await conn.execute(
                    text(
                        "UPDATE runs SET status = :status, ended_at = now(), "
                        "metrics = CAST(:metrics AS jsonb), end_reason = :end_reason, "
                        "cost = CAST(:cost AS jsonb), recording_url = :recording_url "
                        "WHERE id = :id"
                    ),
                    {
                        "id": run_id,
                        "status": final_status,
                        "metrics": json.dumps(metrics),
                        "end_reason": end_reason,
                        "cost": json.dumps(usage.as_dict()),
                        "recording_url": result.recording_url,
                    },
                )
                await maybe_close_parent(conn, run_id)
        except Exception:
            logger.exception(f"discovery finalization failed for run {run_id}")
            await _fail(engine, run_id, "finalize_failed", "could not record the discovery result")
    finally:
        await _shutdown_task(
            heartbeat_task,
            timeout=_HEARTBEAT_SHUTDOWN_TIMEOUT_SECONDS,
            label=f"heartbeat (run {run_id})",
        )


def _summarize(
    graph: FlowGraph, intents: list[IntentSummary], explorer: ExplorerCaller
) -> DiscoveryResult:
    blocked_names = {i.name.casefold() for i in intents if i.state == "blocked"}
    return DiscoveryResult(
        nodes_mapped=sum(1 for n in graph.nodes if n.state == "mapped"),
        nodes_blocked=sum(1 for n in graph.nodes if n.state == "blocked"),
        intents_mapped=sum(1 for i in intents if i.state == "mapped"),
        intents_blocked=sum(1 for i in intents if i.state == "blocked"),
        unexplored=[c for c in explorer.remaining if c.casefold() not in blocked_names],
        turns_used=explorer.caller_turns,
    )
