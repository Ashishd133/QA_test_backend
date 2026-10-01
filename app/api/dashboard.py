"""B1-06 (v1) + B2.8-01/02/03/04 (v2): GET /v1/metrics/dashboard and
GET /v1/metrics/failing-checks.

v1's shape and its 7d/14d-fixed-window behaviour are both kept verbatim in
`DashboardMetrics` for one release (B2.8-01's own ticket text), alongside
the new v2 fields -- one path, one response model, so there's nothing for
a stale consumer to disagree with.

v1 was deliberately unscoped (app/deps.py's own exemption list names this,
"scoped when B2.8 replaces it") and its one query (`FROM runs WHERE
created_at >= :since`, no parent/child distinction) double-counts: a
batch's parent row gets its own rolled-up verdict via rollup.py, so an
unfiltered scan counts both that parent AND each of its children as
separate "calls" for what should be N calls, not N+1. v2 fixes both: every
query here takes `Depends(require_project_id)` and filters to real leaf
calls only via `_LEAF_CALLS_PREDICATE`.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.api.agents import agent_health_from_liveness, derive_agent_liveness
from app.db import get_engine
from app.deps import require_project_id
from app.schemas.dashboard import (
    DashboardMetrics,
    FailingCheck,
    LatencyPercentiles,
    Outcome,
    PostureCards,
    RecentRun,
    SpendSummary,
    SuitePassRateSeries,
    Window,
)
from app.verdict import Verdict, verdict_for_run

router = APIRouter(tags=["dashboard"])

_WINDOW_TO_TIMEDELTA: dict[Window, timedelta] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}

# No stored `latency_threshold_ms` setting exists yet -- pinned here,
# matching the redesign mock's own "1,000 ms target"/threshold copy
# verbatim (Cadence.redesign.html's dashboard section).
_LATENCY_THRESHOLD_MS = 1000

# 12 buckets, 200ms each, 0-2400ms -- the mock's own histogram axis labels
# ("0.2s ... 1.0s ... 2.4s" across 12 bars), not a dynamically scaled range.
_HISTOGRAM_BUCKET_MS = 200
_HISTOGRAM_BUCKETS = 12

_RUN_TITLE_BY_TYPE = {
    "discovery": "Discovery Run",
    "redteam": "Red Team Run",
    "suite": "Suite Run",
}


def _run_title(run_type: str, scenario_name: str | None) -> str:
    return scenario_name or _RUN_TITLE_BY_TYPE.get(run_type, "Run")


@dataclass
class _RunRow:
    created_at: datetime
    status: str
    metrics: dict[str, Any] | None


def _signed(value: float, suffix: str = "") -> str:
    sign = "+" if value >= 0 else ""
    if float(value).is_integer():
        return f"{sign}{int(value)}{suffix}"
    return f"{sign}{value:.1f}{suffix}"


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


def _daily_counts(rows: list[_RunRow], start: datetime, end: datetime) -> list[int]:
    days = (end.date() - start.date()).days
    counts = [0] * (days + 1)
    for row in rows:
        offset = (row.created_at.date() - start.date()).days
        if 0 <= offset < len(counts):
            counts[offset] += 1
    return counts


# A real leaf call is either a genuine child, or a childless Test Run that
# is its own implicit single call (RunAggregate's convention, app/api/
# runs.py). A batch's parent-with-children row is neither -- it carries its
# own rolled-up `status`/`metrics.resultBadge` (rollup.py) for *its own*
# GET, not to be counted again alongside the children it summarizes.
_LEAF_CALLS_PREDICATE = (
    "(r.parent_run_id IS NOT NULL "
    " OR NOT EXISTS (SELECT 1 FROM runs c WHERE c.parent_run_id = r.id))"
)

_LEAF_RUNS_SINCE_SQL = text(
    "SELECT r.created_at, r.status, r.metrics "
    "FROM runs r WHERE r.project_id = :project_id AND r.created_at >= :since "
    f"AND {_LEAF_CALLS_PREDICATE}"
)


@router.get("/v1/metrics/dashboard", response_model=DashboardMetrics)
async def get_dashboard_metrics(
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
    window: Window = Query(default="7d"),
) -> DashboardMetrics:
    now = datetime.now(UTC)
    window_delta = _WINDOW_TO_TIMEDELTA[window]
    current_start = now - window_delta
    prior_start = current_start - window_delta
    # 13, not 14: _daily_counts buckets an *inclusive* [start.date(),
    # end.date()] range, so 13 days back spans exactly 14 distinct days.
    trend_14d_start = now - timedelta(days=13)
    fetch_since = min(trend_14d_start, prior_start)

    async with engine.connect() as conn:
        leaf_rows = (
            (
                await conn.execute(
                    _LEAF_RUNS_SINCE_SQL, {"project_id": project_id, "since": fetch_since}
                )
            )
            .mappings()
            .all()
        )
        total_scenarios = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM scenarios sc JOIN suites s ON s.id = sc.suite_id "
                    "WHERE s.project_id = :project_id"
                ),
                {"project_id": project_id},
            )
        ).scalar_one()
        covered_now = await _scenario_coverage(conn, project_id, before=None)
        covered_prev = await _scenario_coverage(conn, project_id, before=current_start)

        latency_summary = await _fetch_latency_summary(
            conn, project_id, prior_start=prior_start, current_start=current_start
        )
        histogram = await _fetch_latency_histogram(conn, project_id, current_start)
        critical_findings = await _fetch_open_critical_findings(conn, project_id)
        pass_rate_by_suite = await _fetch_pass_rate_by_suite(conn, project_id, trend_14d_start, now)
        spend = await _fetch_spend(conn, project_id, current_start, now)
        recent_runs = await _fetch_recent_runs(conn, project_id)
        liveness = await derive_agent_liveness(conn, project_id)
        agent_rows = (
            (
                await conn.execute(
                    text(
                        "SELECT id, name, transport, language FROM agents "
                        "WHERE project_id = :project_id ORDER BY created_at"
                    ),
                    {"project_id": project_id},
                )
            )
            .mappings()
            .all()
        )

    all_rows = [_RunRow(r["created_at"], r["status"], r["metrics"]) for r in leaf_rows]
    trend_rows = [r for r in all_rows if r.created_at >= trend_14d_start]
    current_rows = [r for r in all_rows if r.created_at >= current_start]
    previous_rows = [r for r in all_rows if prior_start <= r.created_at < current_start]

    trend_bars = _daily_counts(trend_rows, trend_14d_start, now)

    passed, warnings, failed = _outcome_tally(current_rows)
    pass_pct = _pass_pct(passed, warnings, failed)
    prev_passed, prev_warnings, prev_failed = _outcome_tally(previous_rows)
    prev_pass_pct = _pass_pct(prev_passed, prev_warnings, prev_failed)

    avg_latency = _avg_latency_ms(current_rows)
    prev_avg_latency = _avg_latency_ms(previous_rows)

    coverage_now = round(100 * covered_now / total_scenarios) if total_scenarios else 0
    coverage_prev = round(100 * covered_prev / total_scenarios) if total_scenarios else 0

    calls_failed = sum(1 for r in current_rows if r.status == "failed")
    calls_cancelled = sum(1 for r in current_rows if r.status == "cancelled")

    posture = PostureCards(
        pass_rate_pct=float(pass_pct),
        pass_rate_delta_pts=float(pass_pct - prev_pass_pct),
        calls_run=len(current_rows),
        calls_failed=calls_failed,
        calls_cancelled=calls_cancelled,
        latency_p95_ms=latency_summary["p95_current"],
        latency_p95_delta_ms=(
            latency_summary["p95_current"] - latency_summary["p95_prior"]
            if latency_summary["p95_current"] is not None
            and latency_summary["p95_prior"] is not None
            else None
        ),
        latency_threshold_ms=_LATENCY_THRESHOLD_MS,
        open_critical_findings=sum(critical_findings.values()),
        open_critical_findings_by_category=critical_findings,
    )
    latency = LatencyPercentiles(
        p50_ms=latency_summary["p50_current"],
        p90_ms=latency_summary["p90_current"],
        p95_ms=latency_summary["p95_current"],
        threshold_ms=_LATENCY_THRESHOLD_MS,
        total_turns=int(latency_summary["total_turns"] or 0),
        flagged_turns=int(latency_summary["flagged_turns"] or 0),
        histogram=histogram,
    )
    agent_health = [agent_health_from_liveness(row, liveness) for row in agent_rows]

    return DashboardMetrics(
        pass_rate=f"{pass_pct}%",
        pass_rate_delta=_signed(pass_pct - prev_pass_pct, "%"),
        testRuns7d=str(len(current_rows)),
        test_runs_delta=_signed(len(current_rows) - len(previous_rows)),
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
        window=window,
        posture=posture,
        pass_rate_by_suite=pass_rate_by_suite,
        latency=latency,
        spend=spend,
        recent_runs=recent_runs,
        agent_health=agent_health,
    )


async def _scenario_coverage(
    conn: AsyncConnection, project_id: uuid.UUID, *, before: datetime | None
) -> int:
    query = (
        "SELECT count(DISTINCT r.scenario_id) FROM runs r "
        "WHERE r.project_id = :project_id AND r.scenario_id IS NOT NULL "
        f"AND r.status = 'completed' AND {_LEAF_CALLS_PREDICATE}"
    )
    params: dict[str, Any] = {"project_id": project_id}
    if before is not None:
        query += " AND r.created_at < :before"
        params["before"] = before
    return int((await conn.execute(text(query), params)).scalar_one())


_LATENCY_SUMMARY_SQL = text(
    "SELECT "
    "  percentile_cont(0.5) WITHIN GROUP (ORDER BY t.latency_ms) "
    "    FILTER (WHERE r.created_at >= :current_start) AS p50_current, "
    "  percentile_cont(0.9) WITHIN GROUP (ORDER BY t.latency_ms) "
    "    FILTER (WHERE r.created_at >= :current_start) AS p90_current, "
    "  percentile_cont(0.95) WITHIN GROUP (ORDER BY t.latency_ms) "
    "    FILTER (WHERE r.created_at >= :current_start) AS p95_current, "
    "  percentile_cont(0.95) WITHIN GROUP (ORDER BY t.latency_ms) "
    "    FILTER (WHERE r.created_at >= :prior_start AND r.created_at < :current_start) "
    "    AS p95_prior, "
    "  count(*) FILTER (WHERE r.created_at >= :current_start) AS total_turns, "
    "  count(*) FILTER (WHERE r.created_at >= :current_start AND t.latency_ms > :threshold) "
    "    AS flagged_turns "
    "FROM turns t JOIN runs r ON r.id = t.run_id "
    "WHERE r.project_id = :project_id AND r.created_at >= :prior_start "
    f"  AND t.latency_ms IS NOT NULL AND {_LEAF_CALLS_PREDICATE}"
)


async def _fetch_latency_summary(
    conn: AsyncConnection, project_id: uuid.UUID, *, prior_start: datetime, current_start: datetime
) -> dict[str, float | int | None]:
    row = (
        (
            await conn.execute(
                _LATENCY_SUMMARY_SQL,
                {
                    "project_id": project_id,
                    "prior_start": prior_start,
                    "current_start": current_start,
                    "threshold": _LATENCY_THRESHOLD_MS,
                },
            )
        )
        .mappings()
        .one()
    )
    return {
        "p50_current": row["p50_current"],
        "p90_current": row["p90_current"],
        "p95_current": row["p95_current"],
        "p95_prior": row["p95_prior"],
        "total_turns": row["total_turns"] or 0,
        "flagged_turns": row["flagged_turns"] or 0,
    }


_LATENCY_HISTOGRAM_SQL = text(
    "SELECT width_bucket(t.latency_ms, 0, :ceiling, :buckets) AS bucket, count(*) AS n "
    "FROM turns t JOIN runs r ON r.id = t.run_id "
    "WHERE r.project_id = :project_id AND r.created_at >= :current_start "
    f"  AND t.latency_ms IS NOT NULL AND {_LEAF_CALLS_PREDICATE} "
    "GROUP BY bucket"
)


async def _fetch_latency_histogram(
    conn: AsyncConnection, project_id: uuid.UUID, current_start: datetime
) -> list[int]:
    ceiling = _HISTOGRAM_BUCKET_MS * _HISTOGRAM_BUCKETS
    rows = (
        (
            await conn.execute(
                _LATENCY_HISTOGRAM_SQL,
                {
                    "project_id": project_id,
                    "current_start": current_start,
                    "ceiling": ceiling,
                    "buckets": _HISTOGRAM_BUCKETS,
                },
            )
        )
        .mappings()
        .all()
    )
    histogram = [0] * _HISTOGRAM_BUCKETS
    for row in rows:
        # width_bucket returns 0 for values < 0 (impossible here) and
        # buckets+1 for values >= ceiling -- both overflow into the
        # nearest real bucket rather than being dropped.
        bucket = min(max(int(row["bucket"]), 1), _HISTOGRAM_BUCKETS)
        histogram[bucket - 1] += int(row["n"])
    return histogram


_OPEN_CRITICAL_FINDINGS_SQL = text(
    "SELECT f.category, count(*) AS n FROM findings f "
    "JOIN runs r ON r.id = f.run_id "
    "WHERE r.project_id = :project_id AND f.severity = 'critical' AND f.resolved_at IS NULL "
    "GROUP BY f.category"
)


async def _fetch_open_critical_findings(
    conn: AsyncConnection, project_id: uuid.UUID
) -> dict[str, int]:
    rows = (
        (await conn.execute(_OPEN_CRITICAL_FINDINGS_SQL, {"project_id": project_id}))
        .mappings()
        .all()
    )
    return {row["category"]: int(row["n"]) for row in rows}


_PASS_RATE_BY_SUITE_SQL = text(
    "SELECT s.id AS suite_id, s.name AS suite_name, r.created_at, r.status, r.metrics "
    "FROM runs r "
    "JOIN scenarios sc ON sc.id = r.scenario_id "
    "JOIN suites s ON s.id = sc.suite_id "
    "WHERE r.project_id = :project_id AND r.created_at >= :since "
    f"  AND {_LEAF_CALLS_PREDICATE}"
)


async def _fetch_pass_rate_by_suite(
    conn: AsyncConnection, project_id: uuid.UUID, start: datetime, end: datetime
) -> list[SuitePassRateSeries]:
    rows = (
        (await conn.execute(_PASS_RATE_BY_SUITE_SQL, {"project_id": project_id, "since": start}))
        .mappings()
        .all()
    )
    days = (end.date() - start.date()).days + 1
    by_suite: dict[uuid.UUID, tuple[str, list[list[Verdict]]]] = {}
    for row in rows:
        suite_id = row["suite_id"]
        if suite_id not in by_suite:
            by_suite[suite_id] = (row["suite_name"], [[] for _ in range(days)])
        offset = (row["created_at"].date() - start.date()).days
        if 0 <= offset < days:
            verdict = verdict_for_run(row["status"], row["metrics"])
            if verdict != "idle":
                by_suite[suite_id][1][offset].append(verdict)

    series = []
    for suite_id, (suite_name, per_day) in by_suite.items():
        daily_pass_rate: list[float | None] = []
        for verdicts in per_day:
            if not verdicts:
                daily_pass_rate.append(None)
            else:
                daily_pass_rate.append(sum(1 for v in verdicts if v == "pass") / len(verdicts))
        series.append(
            SuitePassRateSeries(
                suite_id=str(suite_id), suite_name=suite_name, daily_pass_rate=daily_pass_rate
            )
        )
    return series


_SPEND_SQL = text(
    "SELECT count(*) AS call_count, sum((cost->>'estUsd')::numeric) AS window_spend "
    "FROM runs r WHERE r.project_id = :project_id AND r.created_at >= :since AND r.cost IS NOT NULL"
)
_MONTH_TO_DATE_SPEND_SQL = text(
    "SELECT sum((cost->>'estUsd')::numeric) AS spend "
    "FROM runs r WHERE r.project_id = :project_id AND r.created_at >= :month_start "
    "  AND r.cost IS NOT NULL"
)
_PROJECT_BUDGET_SQL = text("SELECT budget_usd_monthly FROM projects WHERE id = :project_id")


async def _fetch_spend(
    conn: AsyncConnection, project_id: uuid.UUID, current_start: datetime, now: datetime
) -> SpendSummary:
    row = (
        (await conn.execute(_SPEND_SQL, {"project_id": project_id, "since": current_start}))
        .mappings()
        .one()
    )
    call_count = row["call_count"] or 0
    window_spend = float(row["window_spend"]) if row["window_spend"] is not None else 0.0
    cost_per_call = window_spend / call_count if call_count else None

    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_spend_row = (
        (
            await conn.execute(
                _MONTH_TO_DATE_SPEND_SQL, {"project_id": project_id, "month_start": month_start}
            )
        )
        .mappings()
        .one()
    )
    month_spend = float(month_spend_row["spend"]) if month_spend_row["spend"] is not None else 0.0
    budget_row = (
        (await conn.execute(_PROJECT_BUDGET_SQL, {"project_id": project_id})).mappings().one()
    )
    budget = budget_row["budget_usd_monthly"]
    credits_remaining_pct: float | None
    if budget is None:
        credits_remaining_pct = None
    elif float(budget) <= 0:
        credits_remaining_pct = 0.0
    else:
        credits_remaining_pct = max(0.0, min(100.0, 100.0 * (1 - month_spend / float(budget))))

    return SpendSummary(
        cost_per_call=cost_per_call,
        window_spend=window_spend,
        credits_remaining_pct=credits_remaining_pct,
    )


_RECENT_PARENTS_SQL = text(
    "SELECT r.id, r.type, r.status, r.metrics, r.trigger, r.created_at, "
    "       a.name AS agent_name, own_scenario.name AS own_scenario_name, "
    "       own_suite.name AS own_suite_name "
    "FROM runs r "
    "JOIN agents a ON a.id = r.agent_id "
    "LEFT JOIN scenarios own_scenario ON own_scenario.id = r.scenario_id "
    "LEFT JOIN suites own_suite ON own_suite.id = own_scenario.suite_id "
    "WHERE r.project_id = :project_id AND r.parent_run_id IS NULL "
    "ORDER BY r.created_at DESC LIMIT :limit"
)
_CHILD_ROWS_FOR_PARENTS_SQL = text(
    "SELECT c.parent_run_id, c.status, c.metrics, s.name AS suite_name "
    "FROM runs c "
    "LEFT JOIN scenarios sc ON sc.id = c.scenario_id "
    "LEFT JOIN suites s ON s.id = sc.suite_id "
    "WHERE c.parent_run_id = ANY(:parent_ids)"
)
_RECENT_RUNS_LIMIT = 8


async def _fetch_recent_runs(conn: AsyncConnection, project_id: uuid.UUID) -> list[RecentRun]:
    parents = (
        (
            await conn.execute(
                _RECENT_PARENTS_SQL, {"project_id": project_id, "limit": _RECENT_RUNS_LIMIT}
            )
        )
        .mappings()
        .all()
    )
    if not parents:
        return []
    parent_ids = [p["id"] for p in parents]
    children = (
        (await conn.execute(_CHILD_ROWS_FOR_PARENTS_SQL, {"parent_ids": parent_ids}))
        .mappings()
        .all()
    )
    children_by_parent: dict[uuid.UUID, list[RowMapping]] = {}
    for child in children:
        children_by_parent.setdefault(child["parent_run_id"], []).append(child)

    recent_runs = []
    for parent in parents:
        own_children = children_by_parent.get(parent["id"], [])
        if own_children:
            statuses = [(c["status"], c["metrics"]) for c in own_children]
            suite_name = next((c["suite_name"] for c in own_children if c["suite_name"]), None)
        else:
            statuses = [(parent["status"], parent["metrics"])]
            suite_name = None
        verdicts = [verdict_for_run(status, metrics) for status, metrics in statuses]
        scored = [v for v in verdicts if v != "idle"]
        pass_rate = (sum(1 for v in scored if v == "pass") / len(scored)) if scored else None

        # A batch has no scenario of its own -- the suite name is the only
        # distinguishing label available. A childless single run already has
        # a real scenario (or is discovery/redteam), so it uses the exact
        # same title convention as GET /v1/runs (app/api/runs.py's own
        # `_run_title`) rather than the less specific suite name.
        name = (
            suite_name if own_children else _run_title(parent["type"], parent["own_scenario_name"])
        ) or _run_title(parent["type"], None)
        recent_runs.append(
            RecentRun(
                id=str(parent["id"]),
                name=name,
                agent_name=parent["agent_name"],
                trigger=parent["trigger"],
                call_count=len(statuses),
                pass_rate=pass_rate,
                status=verdict_for_run(parent["status"], parent["metrics"]),
            )
        )
    return recent_runs


# --- B2.8-03: GET /v1/metrics/failing-checks ---

_FAILING_ASSERTIONS_SQL = text(
    "SELECT ar.assertion_id AS check_id, ar.name, "
    "  count(*) FILTER (WHERE ar.status = 'failed') AS fail_count, "
    "  count(*) AS total_count, "
    "  max(r.created_at) FILTER (WHERE ar.status = 'failed') AS last_seen_at "
    "FROM assertion_results ar JOIN runs r ON r.id = ar.run_id "
    "WHERE r.project_id = :project_id AND r.created_at >= :since "
    "GROUP BY ar.assertion_id, ar.name "
    "HAVING count(*) FILTER (WHERE ar.status = 'failed') > 0"
)
_FAILING_METRICS_SQL = text(
    "SELECT mr.metric_id AS check_id, m.name, "
    "  count(*) FILTER (WHERE mr.status = 'failed') AS fail_count, "
    "  count(*) AS total_count, "
    "  max(r.created_at) FILTER (WHERE mr.status = 'failed') AS last_seen_at "
    "FROM metric_results mr "
    "JOIN runs r ON r.id = mr.run_id "
    "JOIN metrics m ON m.id = mr.metric_id "
    "WHERE r.project_id = :project_id AND r.created_at >= :since "
    "GROUP BY mr.metric_id, m.name "
    "HAVING count(*) FILTER (WHERE mr.status = 'failed') > 0"
)


@router.get("/v1/metrics/failing-checks", response_model=list[FailingCheck])
async def get_failing_checks(
    project_id: uuid.UUID = Depends(require_project_id),
    engine: AsyncEngine = Depends(get_engine),
    window: Window = Query(default="7d"),
    limit: int = Query(default=10, ge=1, le=50),
) -> list[FailingCheck]:
    since = datetime.now(UTC) - _WINDOW_TO_TIMEDELTA[window]
    async with engine.connect() as conn:
        assertion_rows = (
            (
                await conn.execute(
                    _FAILING_ASSERTIONS_SQL, {"project_id": project_id, "since": since}
                )
            )
            .mappings()
            .all()
        )
        metric_rows = (
            (await conn.execute(_FAILING_METRICS_SQL, {"project_id": project_id, "since": since}))
            .mappings()
            .all()
        )

    checks = [
        FailingCheck(
            kind="assertion",
            check_id=row["check_id"],
            name=row["name"],
            scope_label="Assertion",
            fail_count=row["fail_count"],
            total_count=row["total_count"],
            rate_pct=round(100 * row["fail_count"] / row["total_count"]),
            last_seen_at=row["last_seen_at"].isoformat() if row["last_seen_at"] else None,
            drilldown_filter=f"failingCheckKind=assertion&failingCheckId={row['check_id']}",
        )
        for row in assertion_rows
    ] + [
        FailingCheck(
            kind="metric",
            check_id=str(row["check_id"]),
            name=row["name"],
            scope_label="Metric",
            fail_count=row["fail_count"],
            total_count=row["total_count"],
            rate_pct=round(100 * row["fail_count"] / row["total_count"]),
            last_seen_at=row["last_seen_at"].isoformat() if row["last_seen_at"] else None,
            drilldown_filter=f"failingCheckKind=metric&failingCheckId={row['check_id']}",
        )
        for row in metric_rows
    ]
    checks.sort(key=lambda c: c.fail_count, reverse=True)
    return checks[:limit]
