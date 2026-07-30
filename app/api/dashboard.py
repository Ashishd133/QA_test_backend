"""B1-06: GET /v1/metrics/dashboard + GET /v1/personas.

All rolling metrics use the same two windows for consistency: the
trailing 7 days (current) vs the 7 days before that (previous, for
deltas); trendBars covers the full trailing 14 days, bucketed by day.
Scenario coverage is a cumulative, not time-windowed, metric (% of all
scenarios with at least one completed run ever) -- its delta compares
that snapshot against what coverage looked like 7 days ago, using the
current scenario count as the denominator both times (scenario counts
change slowly enough that this simplification doesn't skew the number).

No frontend type exists for Persona (nothing consumes it yet -- B1-08
is what actually seeds rows), so its shape just mirrors the DB model.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import get_engine
from app.schemas.dashboard import DashboardMetrics, Outcome, Persona
from app.verdict import verdict_for_run

router = APIRouter(tags=["dashboard"])


@dataclass
class _RunRow:
    created_at: datetime
    status: str
    metrics: dict[str, Any] | None


def _signed(value: int, suffix: str = "") -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}{value}{suffix}"


def _outcome_tally(rows: list[_RunRow]) -> tuple[int, int, int]:
    passed = warnings = failed = 0
    for row in rows:
        verdict = verdict_for_run(row.status, row.metrics)
        if verdict == "pass":
            passed += 1
        elif verdict == "warn":
            warnings += 1
        elif verdict == "fail":
            failed += 1
        # "idle" (still in progress) doesn't count toward any outcome yet.
    return passed, warnings, failed


def _pass_pct(passed: int, warnings: int, failed: int) -> int:
    total = passed + warnings + failed
    return round(100 * passed / total) if total else 0


def _avg_latency_ms(rows: list[_RunRow]) -> float | None:
    values = [
        m["avgLatencyMs"]
        for row in rows
        if (m := row.metrics) and isinstance(m.get("avgLatencyMs"), int | float)
    ]
    return sum(values) / len(values) if values else None


_RECENT_RUNS_SQL = text("SELECT created_at, status, metrics FROM runs WHERE created_at >= :since")


@router.get("/v1/metrics/dashboard", response_model=DashboardMetrics)
async def get_dashboard_metrics(engine: AsyncEngine = Depends(get_engine)) -> DashboardMetrics:
    now = datetime.now(UTC)
    # 13, not 14: _daily_counts buckets an *inclusive* [start.date(), end.date()]
    # range, so 13 days back from today spans exactly 14 distinct calendar days.
    window_14d_start = now - timedelta(days=13)
    window_7d_start = now - timedelta(days=7)

    async with engine.connect() as conn:
        run_rows = (
            (await conn.execute(_RECENT_RUNS_SQL, {"since": window_14d_start})).mappings().all()
        )
        total_scenarios = (await conn.execute(text("SELECT count(*) FROM scenarios"))).scalar_one()
        covered_now = (
            await conn.execute(
                text(
                    "SELECT count(DISTINCT scenario_id) FROM runs "
                    "WHERE scenario_id IS NOT NULL AND status = 'completed'"
                )
            )
        ).scalar_one()
        covered_prev = (
            await conn.execute(
                text(
                    "SELECT count(DISTINCT scenario_id) FROM runs "
                    "WHERE scenario_id IS NOT NULL AND status = 'completed' "
                    "AND created_at < :cutoff"
                ),
                {"cutoff": window_7d_start},
            )
        ).scalar_one()

    all_rows = [_RunRow(r["created_at"], r["status"], r["metrics"]) for r in run_rows]
    current_week = [r for r in all_rows if r.created_at >= window_7d_start]
    previous_week = [r for r in all_rows if r.created_at < window_7d_start]

    trend_bars = _daily_counts(all_rows, window_14d_start, now)

    passed, warnings, failed = _outcome_tally(current_week)
    pass_pct = _pass_pct(passed, warnings, failed)
    prev_passed, prev_warnings, prev_failed = _outcome_tally(previous_week)
    prev_pass_pct = _pass_pct(prev_passed, prev_warnings, prev_failed)

    avg_latency = _avg_latency_ms(current_week)
    prev_avg_latency = _avg_latency_ms(previous_week)

    coverage_now = round(100 * covered_now / total_scenarios) if total_scenarios else 0
    coverage_prev = round(100 * covered_prev / total_scenarios) if total_scenarios else 0

    return DashboardMetrics(
        pass_rate=f"{pass_pct}%",
        pass_rate_delta=_signed(pass_pct - prev_pass_pct, "%"),
        # Keyword must match the field's explicit Field(alias=...) exactly
        # (mypy's dataclass_transform support enforces this for a literal
        # alias, unlike the other fields' dynamic alias_generator=to_camel).
        testRuns7d=str(len(current_week)),
        test_runs_delta=_signed(len(current_week) - len(previous_week)),
        avg_latency=f"{round(avg_latency)}ms" if avg_latency is not None else "-",
        avg_latency_delta=(
            _signed(round(avg_latency - prev_avg_latency), "ms")
            if avg_latency is not None and prev_avg_latency is not None
            else "0ms"
        ),
        scenario_coverage=f"{coverage_now}%",
        scenario_coverage_delta=_signed(coverage_now - coverage_prev, "%"),
        trend_bars=trend_bars,
        outcome=Outcome(passed=passed, warnings=warnings, failed=failed, pass_pct=pass_pct),
    )


def _daily_counts(rows: list[_RunRow], start: datetime, end: datetime) -> list[int]:
    days = (end.date() - start.date()).days
    counts = [0] * (days + 1)
    for row in rows:
        offset = (row.created_at.date() - start.date()).days
        if 0 <= offset < len(counts):
            counts[offset] += 1
    return counts


_PERSONAS_SQL = text(
    "SELECT id, name, voice, language, accent, traits, builtin FROM personas ORDER BY name"
)


@router.get("/v1/personas", response_model=list[Persona])
async def list_personas(engine: AsyncEngine = Depends(get_engine)) -> list[Persona]:
    async with engine.connect() as conn:
        rows = (await conn.execute(_PERSONAS_SQL)).mappings().all()
    return [
        Persona(
            id=str(row["id"]),
            name=row["name"],
            voice=row["voice"],
            language=row["language"],
            accent=row["accent"],
            traits=row["traits"],
            builtin=row["builtin"],
        )
        for row in rows
    ]
