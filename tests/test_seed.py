"""B1-08 tests: python -m app.seed is idempotent.

The ticket's acceptance bar is literally "run it twice -> no duplicates",
so this drives the real `seed()` coroutine twice against the isolated test
DB and asserts exact row counts (not just "some rows exist") for every
seeded table, keyed by the same deterministic ids the script itself uses.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.seed import _AGENTS, _PERSONAS, _SCENARIOS, _SEEDED_RUNS, _SUITES, _id, seed
from tests.conftest import _test_engine, requires_test_db

pytestmark = requires_test_db


async def _cleanup(engine: AsyncEngine) -> None:
    run_ids = [_id(r["key"]) for r in _SEEDED_RUNS]
    async with engine.connect() as conn, conn.begin():
        for run_id in run_ids:
            await conn.execute(text("DELETE FROM turns WHERE run_id = :id"), {"id": run_id})
            await conn.execute(
                text("DELETE FROM assertion_results WHERE run_id = :id"), {"id": run_id}
            )
            await conn.execute(text("DELETE FROM run_events WHERE run_id = :id"), {"id": run_id})
        await conn.execute(text("DELETE FROM runs WHERE id = ANY(:ids)"), {"ids": run_ids})
        await conn.execute(
            text("DELETE FROM scenarios WHERE id = ANY(:ids)"),
            {"ids": [_id(s["key"]) for s in _SCENARIOS]},
        )
        await conn.execute(
            text("DELETE FROM suites WHERE id = ANY(:ids)"),
            {"ids": [_id(s["key"]) for s in _SUITES]},
        )
        await conn.execute(
            text("DELETE FROM personas WHERE id = ANY(:ids)"),
            {"ids": [_id(p["key"]) for p in _PERSONAS]},
        )
        await conn.execute(
            text("DELETE FROM agents WHERE id = ANY(:ids)"),
            {"ids": [_id(a["key"]) for a in _AGENTS]},
        )


async def test_seed_is_idempotent_across_two_runs() -> None:
    engine = _test_engine()
    try:
        await seed(engine)
        await seed(engine)

        async with engine.connect() as conn:
            agent_count = (
                await conn.execute(
                    text("SELECT count(*) FROM agents WHERE id = ANY(:ids)"),
                    {"ids": [_id(a["key"]) for a in _AGENTS]},
                )
            ).scalar_one()
            suite_count = (
                await conn.execute(
                    text("SELECT count(*) FROM suites WHERE id = ANY(:ids)"),
                    {"ids": [_id(s["key"]) for s in _SUITES]},
                )
            ).scalar_one()
            scenario_count = (
                await conn.execute(
                    text("SELECT count(*) FROM scenarios WHERE id = ANY(:ids)"),
                    {"ids": [_id(s["key"]) for s in _SCENARIOS]},
                )
            ).scalar_one()
            persona_count = (
                await conn.execute(
                    text("SELECT count(*) FROM personas WHERE id = ANY(:ids)"),
                    {"ids": [_id(p["key"]) for p in _PERSONAS]},
                )
            ).scalar_one()
            run_rows = (
                (
                    await conn.execute(
                        text("SELECT status FROM runs WHERE id = ANY(:ids)"),
                        {"ids": [_id(r["key"]) for r in _SEEDED_RUNS]},
                    )
                )
                .scalars()
                .all()
            )
            turn_count = (
                await conn.execute(
                    text("SELECT count(*) FROM turns WHERE run_id = ANY(:ids)"),
                    {"ids": [_id(r["key"]) for r in _SEEDED_RUNS]},
                )
            ).scalar_one()

        assert agent_count == len(_AGENTS)
        assert suite_count == len(_SUITES)
        assert scenario_count == len(_SCENARIOS)
        assert persona_count == len(_PERSONAS)
        assert len(run_rows) == len(_SEEDED_RUNS)
        assert all(status == "completed" for status in run_rows)
        # basic_simulation.json has 4 turn events -- 4 runs * 4 turns, not 8,
        # proves the second seed() call didn't re-run FakeRunner and
        # duplicate materialized rows.
        assert turn_count == len(_SEEDED_RUNS) * 4
    finally:
        await _cleanup(engine)
