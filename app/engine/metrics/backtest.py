"""B2.7-10: backtest a draft metric against past completed calls'
already-recorded transcripts -- no new call is placed (no `run_persona_
call` invocation; this reads only what `turns`/`run_events` already
hold). `llm_judge` metrics go through the exact same `FinalJudge.evaluate`
call shape B2.7-06's production scoring uses (a single-metric
`CompiledMetricSignal`, batched into a final-pass render with no
assertions); `builtin`/`python` metrics call the exact same
app.engine.metrics.compiler functions production scoring calls. Backtest
verdicts are produced by the identical code path, not a parallel
reimplementation that could silently drift from what a live run would
actually score.
"""

from __future__ import annotations

import hashlib
import json
import uuid

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from app.engine.judge.judge import FinalJudge
from app.engine.judge.models import CompiledMetricSignal
from app.engine.metrics.compiler import (
    MetricOutcome,
    evaluate_builtin_metric,
    evaluate_python_metric,
    outcomes_from_judge_verdicts,
)
from app.schemas.runs import TranscriptTurn

# `filters` keys this reads: "agentId" (str, an agent id to scope the
# historical sample to) -- anything else is accepted and ignored, since
# the ticket's own filters shape is intentionally loose/growable.
_HISTORICAL_RUN_IDS_SQL = text(
    "SELECT id FROM runs "
    "WHERE project_id = :project_id AND type = 'simulation' AND status = 'completed' "
    "  AND (CAST(:agent_id AS uuid) IS NULL OR agent_id = CAST(:agent_id AS uuid)) "
    "ORDER BY created_at DESC LIMIT :sample_size"
)

_TURNS_FOR_RUN_SQL = text(
    "SELECT role, text, latency_ms FROM turns WHERE run_id = :run_id ORDER BY idx"
)


def spec_hash(spec: dict[str, object]) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()


async def _historical_run_ids(
    conn: AsyncConnection, *, project_id: uuid.UUID, sample_size: int, filters: dict[str, object]
) -> list[uuid.UUID]:
    agent_id = filters.get("agentId")
    rows = (
        await conn.execute(
            _HISTORICAL_RUN_IDS_SQL,
            {
                "project_id": project_id,
                "agent_id": str(agent_id) if agent_id else None,
                "sample_size": sample_size,
            },
        )
    ).scalars()
    return list(rows)


async def _load_transcript_and_latencies(
    conn: AsyncConnection, run_id: uuid.UUID
) -> tuple[list[TranscriptTurn], list[float]]:
    rows = (await conn.execute(_TURNS_FOR_RUN_SQL, {"run_id": run_id})).mappings().all()
    transcript = [TranscriptTurn(role=r["role"], text=r["text"]) for r in rows]
    latencies = [float(r["latency_ms"]) for r in rows if r["latency_ms"] is not None]
    return transcript, latencies


async def _evaluate_one(
    judge: FinalJudge | None,
    metric_row: RowMapping,
    transcript: list[TranscriptTurn],
    latencies_ms: list[float],
) -> MetricOutcome:
    kind = metric_row["kind"]
    metric_id, version = str(metric_row["id"]), metric_row["version"]

    if kind == "llm_judge":
        if judge is None:
            # Defensive only -- the API layer (app.api.metrics.
            # backtest_metric) only omits `judge` for a non-llm_judge
            # metric, so this branch is unreachable through that call site.
            return MetricOutcome(metric_id, version, "error", None, [], "no judge client available")
        spec = metric_row["spec"] or {}
        signal = CompiledMetricSignal(
            metric_id=metric_id,
            name=metric_row["name"],
            description=str(spec.get("description", "")),
            distinguish_from=str(spec.get("distinguish_from", "")),
        )
        verdict = await judge.evaluate([], transcript, metrics=[signal])
        outcomes = outcomes_from_judge_verdicts(verdict.metrics, [metric_row])
        return (
            outcomes[0]
            if outcomes
            else MetricOutcome(
                metric_id, version, "error", None, [], "judge returned no verdict for this metric"
            )
        )
    if kind == "builtin":
        return evaluate_builtin_metric(metric_row, transcript, latencies_ms)
    if kind == "python":
        context: dict[str, object] = {
            "transcript": [{"role": t.role, "text": t.text} for t in transcript]
        }
        return await evaluate_python_metric(metric_row, context)
    return MetricOutcome(metric_id, version, "error", None, [], f"unrecognized kind {kind!r}")


async def run_backtest(
    conn: AsyncConnection,
    judge: FinalJudge | None,
    metric_row: RowMapping,
    *,
    project_id: uuid.UUID,
    sample_size: int,
    filters: dict[str, object],
) -> list[tuple[uuid.UUID, MetricOutcome]]:
    run_ids = await _historical_run_ids(
        conn, project_id=project_id, sample_size=sample_size, filters=filters
    )
    results: list[tuple[uuid.UUID, MetricOutcome]] = []
    for run_id in run_ids:
        transcript, latencies_ms = await _load_transcript_and_latencies(conn, run_id)
        outcome = await _evaluate_one(judge, metric_row, transcript, latencies_ms)
        results.append((run_id, outcome))
    return results
