"""B5-04: mark a completed discovery run as the curated demo replay (or
unmark it with --off). In a `demo` environment, starting discovery replays
the flagged run's events at their original cadence instead of exploring
live -- see app/engine/executor/discovery_replay.py.

Usage: uv run python -m scripts.flag_demo_replay <run_id> [--off]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


async def flag(engine: AsyncEngine, run_id: uuid.UUID, *, on: bool) -> int:
    """Returns a process exit code (0 done, 1 not eligible)."""
    async with engine.connect() as conn, conn.begin():
        row = (
            (
                await conn.execute(
                    text("SELECT type, status FROM runs WHERE id = :id FOR UPDATE"), {"id": run_id}
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            print(f"no such run: {run_id}", file=sys.stderr)
            return 1
        if row["type"] != "discovery" or row["status"] != "completed":
            print(
                f"run {run_id} is a {row['status']} {row['type']} run; "
                "only a completed discovery run can be a demo replay",
                file=sys.stderr,
            )
            return 1
        await conn.execute(
            text("UPDATE runs SET config = config || CAST(:flag AS jsonb) WHERE id = :id"),
            {"id": run_id, "flag": json.dumps({"demoReplay": on})},
        )
    print(f"run {run_id}: demoReplay={'on' if on else 'off'}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", type=uuid.UUID)
    parser.add_argument("--off", action="store_true", help="remove the flag")
    args = parser.parse_args()

    from app.db import get_engine

    sys.exit(asyncio.run(flag(get_engine(), args.run_id, on=not args.off)))


if __name__ == "__main__":
    main()
