"""B2-09: pretty-print any run's full event history straight from
run_events -- a debugging/audit CLI, not part of the app's own runtime.

Reuses the same per-type interpretation as app/api/runs.py's
_reduce_events(), but prints every event in arrival order (including
status/metrics/error, which that reducer folds away or discards) rather
than collapsing them into a RunDetail response.

Usage: uv run python -m scripts.replay_run <run_id>
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncEngine

_RUN_HEADER_SQL = text(
    "SELECT r.id, r.type, r.status, r.end_reason, r.created_at, r.started_at, r.ended_at, "
    "a.name AS agent_name, sc.name AS scenario_name "
    "FROM runs r "
    "LEFT JOIN agents a ON a.id = r.agent_id "
    "LEFT JOIN scenarios sc ON sc.id = r.scenario_id "
    "WHERE r.id = :id"
)
_RUN_EVENTS_SQL = text(
    "SELECT seq, type, data, ts FROM run_events WHERE run_id = :run_id ORDER BY seq"
)


def _fmt_ts(ts: datetime) -> str:
    return ts.strftime("%H:%M:%S.%f")[:-3]


def _detail_for(event_type: str, data: dict[str, Any]) -> str:
    """The per-type line body -- kept as a pure function of (type, data) so
    it's unit-testable against hand-built event dicts with no DB involved
    (see tests/test_replay_run.py)."""
    if event_type == "status":
        return f"status={data.get('status')}"

    if event_type == "turn":
        lat = data.get("latencyMs")
        lat_str = f"  ({round(lat)}ms)" if isinstance(lat, int | float) else ""
        flag = "  [FLAGGED]" if data.get("flagged") else ""
        role = data.get("role", "?")
        return f"[{role:>6}] {data.get('text', '')!r}{lat_str}{flag}"

    if event_type == "metrics":
        parts = []
        if (score := data.get("score")) is not None:
            parts.append(f"score={score}")
        if (avg := data.get("avgLatencyMs")) is not None:
            parts.append(f"avgLatency={round(avg)}ms")
        if (turns := data.get("turnsCompleted")) is not None:
            parts.append(f"turns={turns}")
        if (interruptions := data.get("interruptions")) is not None:
            parts.append(f"interruptions={interruptions}")
        return " ".join(parts) if parts else "(empty)"

    if event_type == "assertion":
        mark = "✓" if data.get("status") == "passed" else "✗"
        turn_ref = data.get("triggeredAtTurn")
        turn_str = f" @turn {turn_ref}" if turn_ref is not None else ""
        note = data.get("note")
        note_str = f" -- {note}" if note else ""
        assertion_id = data.get("assertionId")
        name = data.get("name")
        return f"{mark} {data.get('status')}  {assertion_id} {name!r}{turn_str}{note_str}"

    if event_type == "done":
        parts = []
        if (score := data.get("score")) is not None:
            parts.append(f"score={score}")
        if (badge := data.get("resultBadge")) is not None:
            parts.append(f"badge={badge}")
        if (findings := data.get("findingsCount")) is not None:
            parts.append(f"findings={findings}")
        if (exposure := data.get("exposure")) is not None:
            parts.append(f"exposure={exposure}")
        return " ".join(parts) if parts else "(no result)"

    if event_type == "error":
        fatal = " [FATAL]" if data.get("fatal") else ""
        return f"{data.get('code')}{fatal}: {data.get('message')}"

    if event_type == "node":
        blocked = f" blocked={data.get('blockedReason')}" if data.get("blockedReason") else ""
        return f"{data.get('nodeId')} {data.get('label')!r} state={data.get('state')}{blocked}"

    if event_type == "intent":
        reason = f" ({data.get('reason')})" if data.get("reason") else ""
        return f"{data.get('name')} state={data.get('state')} path={data.get('path')}{reason}"

    if event_type == "attack":
        verdict = data.get("verdict") or "?"
        return (
            f"[{data.get('category')}] verdict={verdict} "
            f"attack={data.get('attackPrompt')!r} response={data.get('agentResponse')!r}"
        )

    if event_type == "exposure":
        return f"counts={data.get('counts')}"

    # Unknown future event type: don't crash the replay over it, just dump
    # the raw payload so nothing is silently lost.
    return repr(data)


def format_event(seq: int, ts: datetime, event_type: str, data: dict[str, Any]) -> str:
    return f"[{seq:>4}] {_fmt_ts(ts)}  {event_type:<10} {_detail_for(event_type, data)}"


def format_header(row: RowMapping) -> str:
    lines = [
        f"run {row['id']}  type={row['type']}  status={row['status']}"
        f"  end_reason={row['end_reason']}",
        f"agent={row['agent_name']}  scenario={row['scenario_name']}",
        f"created={row['created_at']}  started={row['started_at']}  ended={row['ended_at']}",
        "-" * 72,
    ]
    return "\n".join(lines)


async def replay(engine: AsyncEngine, run_id: uuid.UUID) -> int:
    """Returns a process exit code (0 found/printed, 1 run not found)."""
    async with engine.connect() as conn:
        header_row = (await conn.execute(_RUN_HEADER_SQL, {"id": run_id})).mappings().first()
        if header_row is None:
            print(f"no such run: {run_id}", file=sys.stderr)
            return 1
        event_rows = (await conn.execute(_RUN_EVENTS_SQL, {"run_id": run_id})).mappings().all()

    print(format_header(header_row))
    if not event_rows:
        print("(no events)")
        return 0
    for row in event_rows:
        print(format_event(row["seq"], row["ts"], row["type"], row["data"]))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", type=uuid.UUID, help="the run to replay")
    args = parser.parse_args()

    from app.db import get_engine

    exit_code = asyncio.run(replay(get_engine(), args.run_id))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
