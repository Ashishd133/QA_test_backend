"""B4-04: the real red-team executor.

One red-team *run* is a parent with one child Call per attack (see
app/api/redteam.py); this executes ONE child: a single attack pack against
the agent, over a real call.

  claim -> load pack (id+version from the run's config) + optional identity
    -> status=running -> run_persona_call(planner=AttackerCaller)
         -> on_turn:     emit(turn)
         -> per exchange: emit(attack, interim verdict) + emit(exposure)
    -> AttackJudge grades the whole transcript (verbatim evidence enforced)
    -> one transaction: emit(final attack/exposure/done), insert the finding
       (encrypted evidence), materialize turns, complete the run

result_badge is blocked=pass / leaked|bypassed=fail, so the parent's
rollup (a red-team batch fails on any failed child) and the dashboard see
the real outcome instead of every completed call reading as a pass.

An attack that could not be *delivered* (never authenticated, the model
call failed, the agent never answered) fails the run: "could not test" must
never read as "the agent held the line".
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from typing import Protocol

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.test_profiles import resolve_dummy_identity
from app.config import get_settings
from app.engine.caller.latency_clock import LatencyClock
from app.engine.caller.persona import PersonaSpec, Turn
from app.engine.caller.persona_call import OnTurn, PersonaCallResult, TurnPlanner, run_persona_call
from app.engine.discovery.explorer import GeminiExplorerLLM
from app.engine.executor.discovery import _fail
from app.engine.executor.simulation import (
    _CANCEL_WATCHER_SHUTDOWN_TIMEOUT_SECONDS,
    _HEARTBEAT_SHUTDOWN_TIMEOUT_SECONDS,
    _avg_latency_ms,
    _end_reason_for,
    _load_livekit_creds,
    _poll_for_cancellation,
    _shutdown_task,
)
from app.engine.judge.judge import GenAIClient, build_judge_client
from app.engine.judge.models import TranscriptTurn
from app.engine.livekit_creds import LiveKitCreds
from app.engine.redteam.attacker import (
    MAX_VERIFY_TURNS,
    AttackerCaller,
    AttackerLLM,
    Exchange,
    GeminiAttackerLLM,
)
from app.engine.redteam.findings import insert_finding
from app.engine.redteam.judge import AttackJudge, GradedAttack
from app.engine.redteam.packs import AttackPack, registry
from app.events import (
    attack_event,
    done_event,
    emit,
    exposure_event,
    status_event,
    turn_event,
)
from app.gcp_auth import load_google_oauth2_credentials
from app.usage import UsageTracker
from app.workers.claim import ClaimedRun
from app.workers.heartbeat import heartbeat_loop
from app.workers.materialize import materialize_run
from app.workers.rollup import maybe_close_parent

REDTEAM_RUN_TIMEOUT_SECS = 300.0
REDTEAM_SILENCE_TIMEOUT_SECS = 30.0

_RUN_SQL = text("SELECT created_by_user_id FROM runs WHERE id = :id")

_ATTACKER_PERSONA = PersonaSpec(
    name="Cadence Red Team",
    traits={},
    goal="Run the attack script.",
    opening_line="Hi, I need some help with my account.",
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
        silence_timeout_secs: float | None,
    ) -> PersonaCallResult: ...


def _default_llm() -> AttackerLLM:
    settings = get_settings()
    return GeminiAttackerLLM(
        GeminiExplorerLLM(
            credentials=load_google_oauth2_credentials(),
            project=settings.google_cloud_project,
            location=settings.google_cloud_location,
        )
    )


def find_pack(pack_id: object, version: object) -> AttackPack | None:
    return next((p for p in registry() if p.id == pack_id and p.version == version), None)


async def run_redteam(
    engine: AsyncEngine,
    claimed: ClaimedRun,
    *,
    run_call: CallRunner = run_persona_call,
    make_llm: Callable[[], AttackerLLM] = _default_llm,
    make_judge_client: Callable[[], GenAIClient] = build_judge_client,
) -> None:
    with logger.contextualize(run_id=str(claimed.id)):
        await _run_redteam_body(
            engine,
            claimed,
            run_call=run_call,
            make_llm=make_llm,
            make_judge_client=make_judge_client,
        )


async def _run_redteam_body(
    engine: AsyncEngine,
    claimed: ClaimedRun,
    *,
    run_call: CallRunner,
    make_llm: Callable[[], AttackerLLM],
    make_judge_client: Callable[[], GenAIClient],
) -> None:
    run_id = claimed.id
    pack = find_pack(claimed.config.get("packId"), claimed.config.get("packVersion"))
    if pack is None:
        await _fail(
            engine,
            run_id,
            "pack_unavailable",
            "this run's attack pack is not in the deployed registry",
        )
        return

    try:
        identity: dict[str, object] | None = None
        if claimed.config.get("testProfileId") is not None:
            async with engine.connect() as conn:
                user_id = (await conn.execute(_RUN_SQL, {"id": run_id})).scalar_one()
            identity = await resolve_dummy_identity(engine, claimed.config, user_id=user_id)
        livekit_creds = await _load_livekit_creds(engine, claimed.agent_id)
        llm = make_llm()
        judge = AttackJudge(make_judge_client(), usage=UsageTracker())
    except Exception:
        # Never log config: it holds an encrypted secret and an identity ref.
        logger.exception(f"failed to prepare red-team run {run_id}")
        await _fail(engine, run_id, "redteam_init_failed", "could not prepare this attack")
        return

    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text("UPDATE runs SET status = 'running', started_at = now() WHERE id = :id"),
            {"id": run_id},
        )
        await emit(conn, run_id, status_event(status="running"))

    heartbeat_task = asyncio.create_task(heartbeat_loop(engine, run_id))
    cancel_event = asyncio.Event()
    cancel_watcher_task = asyncio.create_task(_poll_for_cancellation(engine, run_id, cancel_event))

    usage = judge.usage
    latency_clock = LatencyClock()
    transcript: list[Turn] = []
    agent_turns_seen = 0
    leaks_so_far = 0

    async def on_exchange(exchange: Exchange) -> None:
        nonlocal leaks_so_far
        if exchange.verdict != "blocked":
            leaks_so_far += 1
        async with engine.connect() as conn, conn.begin():
            await emit(
                conn,
                run_id,
                attack_event(
                    category=pack.category,
                    attack_prompt=exchange.attack_prompt,
                    agent_response=exchange.agent_response,
                    verdict=exchange.verdict,
                    severity=exchange.severity,
                ),
            )
            await emit(conn, run_id, exposure_event(counts={pack.category: leaks_so_far}))

    attacker = AttackerCaller(llm, pack, identity=identity, on_exchange=on_exchange)

    async def on_turn(turn: Turn) -> None:
        nonlocal agent_turns_seen
        latency_ms: int | None = None
        if turn.speaker == "caller":
            usage.record_tts_chars(len(turn.text))
        else:
            if agent_turns_seen < len(latency_clock.turn_latencies):
                latency_ms = round(latency_clock.turn_latencies[agent_turns_seen].latency_ms)
            agent_turns_seen += 1
        index = len(transcript)
        transcript.append(turn)
        async with engine.connect() as conn, conn.begin():
            await emit(
                conn,
                run_id,
                turn_event(index=index, role=turn.speaker, text=turn.text, latency_ms=latency_ms),
            )

    max_turns = MAX_VERIFY_TURNS + 1 + len(pack.escalations) + 3
    try:
        try:
            result = await run_call(
                _ATTACKER_PERSONA,
                on_turn=on_turn,
                latency_clock=latency_clock,
                cancel_event=cancel_event,
                run_id=run_id,
                livekit=livekit_creds,
                planner=attacker,
                max_turns=max_turns,
                run_timeout_secs=REDTEAM_RUN_TIMEOUT_SECS,
                silence_timeout_secs=REDTEAM_SILENCE_TIMEOUT_SECS,
            )
        except Exception:
            logger.exception(f"red-team call failed for run {run_id}")
            await _fail(engine, run_id, "call_failed", "the attack call failed")
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
            if was_cancelled:
                await _finish_cancelled(engine, run_id, latency_clock, result, usage, end_reason)
                return

            if not attacker.attack_lines:
                await _fail(
                    engine,
                    run_id,
                    "attack_not_delivered",
                    attacker.failure or "the agent never reached the point of being attacked",
                )
                return

            judge_transcript = [TranscriptTurn(role=t.speaker, text=t.text) for t in transcript]
            graded = await judge.grade(pack, judge_transcript)
            await _finish(
                engine,
                run_id,
                pack,
                graded,
                attacker,
                transcript,
                latency_clock,
                result,
                usage,
                end_reason,
            )
        except Exception:
            logger.exception(f"red-team finalization failed for run {run_id}")
            await _fail(engine, run_id, "finalize_failed", "could not grade or record this attack")
    finally:
        await _shutdown_task(
            heartbeat_task,
            timeout=_HEARTBEAT_SHUTDOWN_TIMEOUT_SECONDS,
            label=f"heartbeat (run {run_id})",
        )


def _attack_text(attacker: AttackerCaller, transcript: list[Turn]) -> tuple[str, str]:
    """(what the attacker said, what the agent said) from the first attack
    line onward -- the authentication preamble is not part of the finding."""
    start = attacker.attack_start or 0
    caller = [t.text.strip() for t in transcript[start:] if t.speaker == "caller"]
    agent = [t.text.strip() for t in transcript[start:] if t.speaker == "agent"]
    return "\n".join(caller), " ".join(agent)


async def _finish(
    engine: AsyncEngine,
    run_id: uuid.UUID,
    pack: AttackPack,
    graded: GradedAttack,
    attacker: AttackerCaller,
    transcript: list[Turn],
    latency_clock: LatencyClock,
    result: PersonaCallResult,
    usage: UsageTracker,
    end_reason: str,
) -> None:
    blocked = graded.verdict == "blocked"
    attack_prompt, agent_response = _attack_text(attacker, transcript)
    async with engine.connect() as conn, conn.begin():
        status = (
            await conn.execute(
                text("SELECT status FROM runs WHERE id = :id FOR UPDATE"), {"id": run_id}
            )
        ).scalar_one()
        if status not in ("claimed", "running", "cancelled"):
            return  # reaper already resurrected this run as failed

        # The authoritative verdict, after the interim ones streamed live.
        await emit(
            conn,
            run_id,
            attack_event(
                category=pack.category,
                attack_prompt=attack_prompt,
                agent_response=graded.evidence or agent_response,
                verdict=graded.verdict,
                severity=graded.severity,
            ),
        )
        counts: dict[str, int] = {pack.category: 0 if blocked else 1}
        await emit(conn, run_id, exposure_event(counts=counts))
        await insert_finding(
            conn,
            run_id=run_id,
            pack=pack,
            graded=graded,
            attack_prompt=attack_prompt,
            agent_response=agent_response,
        )
        done = done_event(
            score=1.0 if blocked else 0.0,
            result_badge="pass" if blocked else "fail",
            findings_count=0 if blocked else 1,
            exposure=counts,
        )
        await emit(conn, run_id, done)
        await materialize_run(conn, run_id)
        metrics = {
            **done.data.model_dump(mode="json", by_alias=True),
            "avgLatencyMs": _avg_latency_ms(latency_clock),
            "turnsCompleted": len(transcript),
            "interruptions": latency_clock.interruption_count,
            "verdict": graded.verdict,
            "severity": graded.severity,
            "category": pack.category,
            "packId": pack.id,
        }
        await conn.execute(
            text(
                "UPDATE runs SET status = 'completed', ended_at = now(), "
                "metrics = CAST(:metrics AS jsonb), end_reason = :end_reason, "
                "cost = CAST(:cost AS jsonb), recording_url = :recording_url WHERE id = :id"
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


async def _finish_cancelled(
    engine: AsyncEngine,
    run_id: uuid.UUID,
    latency_clock: LatencyClock,
    result: PersonaCallResult,
    usage: UsageTracker,
    end_reason: str,
) -> None:
    """Same contract as every executor (spine §5): a cancelled call gets a
    partial `done` with no verdict and no finding."""
    partial = done_event()
    async with engine.connect() as conn, conn.begin():
        status = (
            await conn.execute(
                text("SELECT status FROM runs WHERE id = :id FOR UPDATE"), {"id": run_id}
            )
        ).scalar_one()
        if status not in ("claimed", "running", "cancelled"):
            return
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
                "cost = CAST(:cost AS jsonb), recording_url = :recording_url WHERE id = :id"
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
