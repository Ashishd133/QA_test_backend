"""B1-07 tests: materialization of turns/assertion_results on `done`.

The ticket's acceptance bar is a property test -- "replaying detail-from-
tables vs reduce-from-events yields identical JSON" -- so these drive a
real run_fake_script() to completion (and, separately, to cancellation)
and assert the materialized tables reconstruct into exactly the same
TranscriptTurn/ResultAssertion JSON that GET /v1/runs/{id}'s reduce-from-
events path returns.
"""

import asyncio
import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncEngine

from app.main import app
from app.schemas.runs import ResultAssertion, TranscriptTurn
from app.workers.claim import claim_run
from app.workers.fake_runner import run_fake_script
from tests.conftest import _test_engine, auth_headers, requires_test_db

pytestmark = requires_test_db

_HEADERS = auth_headers()


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_HEADERS)


async def _make_agent(engine: AsyncEngine, *, max_concurrency: int = 5) -> uuid.UUID:
    agent_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO agents (id, name, transport, max_concurrency, created_by_user_id) "
                "VALUES (:id, 'Materialization Agent', 'web', :max_concurrency, 'user-1')"
            ),
            {"id": agent_id, "max_concurrency": max_concurrency},
        )
    return agent_id


async def _make_queued_run(engine: AsyncEngine, agent_id: uuid.UUID) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO runs (id, type, agent_id, created_by_user_id) "
                "VALUES (:id, 'simulation', :agent_id, 'user-1')"
            ),
            {"id": run_id, "agent_id": agent_id},
        )
    return run_id


async def _cleanup(engine: AsyncEngine, agent_id: uuid.UUID) -> None:
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text("DELETE FROM turns WHERE run_id IN (SELECT id FROM runs WHERE agent_id = :id)"),
            {"id": agent_id},
        )
        await conn.execute(
            text(
                "DELETE FROM assertion_results "
                "WHERE run_id IN (SELECT id FROM runs WHERE agent_id = :id)"
            ),
            {"id": agent_id},
        )
        await conn.execute(
            text(
                "DELETE FROM run_events WHERE run_id IN (SELECT id FROM runs WHERE agent_id = :id)"
            ),
            {"id": agent_id},
        )
        await conn.execute(text("DELETE FROM runs WHERE agent_id = :id"), {"id": agent_id})
        await conn.execute(text("DELETE FROM agents WHERE id = :id"), {"id": agent_id})
    await engine.dispose()


def _turn_from_row(row: RowMapping) -> dict[str, Any]:
    latency_ms = row["latency_ms"]
    return TranscriptTurn(
        role=row["role"],
        text=row["text"],
        lat=f"{round(latency_ms)}ms" if isinstance(latency_ms, int | float) else None,
        flag=row["flagged"],
        flag_text=row["flag_reason"],
    ).model_dump(mode="json", by_alias=True)


def _assertion_from_row(row: RowMapping) -> dict[str, Any]:
    triggered_at_turn = row["triggered_at_turn"]
    detail = f"Triggered at turn {triggered_at_turn}" if triggered_at_turn is not None else ""
    return ResultAssertion(
        text=row["name"], detail=detail, ok=row["status"] == "passed"
    ).model_dump(mode="json", by_alias=True)


async def _materialized_rows(
    engine: AsyncEngine, run_id: uuid.UUID
) -> tuple[list[RowMapping], list[RowMapping]]:
    async with engine.connect() as conn:
        turn_rows = (
            (
                await conn.execute(
                    text(
                        "SELECT role, text, latency_ms, flagged, flag_reason "
                        "FROM turns WHERE run_id = :id ORDER BY idx"
                    ),
                    {"id": run_id},
                )
            )
            .mappings()
            .all()
        )
        assertion_rows = (
            (
                await conn.execute(
                    text(
                        "SELECT assertion_id, name, status, triggered_at_turn "
                        "FROM assertion_results WHERE run_id = :id"
                    ),
                    {"id": run_id},
                )
            )
            .mappings()
            .all()
        )
    return list(turn_rows), list(assertion_rows)


@pytest.mark.timeout(300)
async def test_materialized_tables_match_reduce_from_events_after_completion() -> None:
    """Same rationale as test_runs_read.py's identical marker: run_fake_script
    opens a fresh DB connection per turn (no pooling in tests), and against
    this environment's degraded network to the Neon test branch that's
    routinely close to or over the default 120s pytest-timeout with nothing
    actually stuck. Confirmed pre-existing via a pre-B2.5 baseline run."""
    engine = _test_engine()
    agent_id = await _make_agent(engine)
    await _make_queued_run(engine, agent_id)
    try:
        claimed = await claim_run(engine, "test-worker-materialize")
        assert claimed is not None
        await run_fake_script(engine, claimed)

        async with await _client() as client:
            response = await client.get(f"/v1/runs/{claimed.id}")
        assert response.status_code == 200
        body = response.json()

        turn_rows, assertion_rows = await _materialized_rows(engine, claimed.id)
        assert len(turn_rows) == 4  # basic_simulation.json's turn count
        assert len(assertion_rows) == 2

        assert [_turn_from_row(row) for row in turn_rows] == body["transcript"]

        # assertion_results has no ordinal column (PK is run_id, assertion_id),
        # so order isn't guaranteed to match the event log's first-appearance
        # order -- compare as sets keyed by assertion text instead.
        materialized_assertions = {
            row["assertion_id"]: _assertion_from_row(row) for row in assertion_rows
        }
        body_assertions_by_text = {a["text"]: a for a in body["resultAssertions"]}
        assert {a["text"]: a for a in materialized_assertions.values()} == body_assertions_by_text
    finally:
        await _cleanup(engine, agent_id)


async def test_materialized_tables_match_reduce_from_events_after_cancellation() -> None:
    engine = _test_engine()
    agent_id = await _make_agent(engine)
    await _make_queued_run(engine, agent_id)
    try:
        claimed = await claim_run(engine, "test-worker-materialize-cancel")
        assert claimed is not None
        run_task = asyncio.create_task(run_fake_script(engine, claimed))

        for _ in range(40):
            async with engine.connect() as conn:
                event_count = (
                    await conn.execute(
                        text("SELECT count(*) FROM run_events WHERE run_id = :id"),
                        {"id": claimed.id},
                    )
                ).scalar_one()
            if event_count >= 2:
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("script never emitted events within the poll window")

        async with await _client() as client:
            cancel_response = await client.post(f"/v1/runs/{claimed.id}/cancel")
        assert cancel_response.status_code == 204

        await asyncio.wait_for(run_task, timeout=15.0)

        async with await _client() as client:
            response = await client.get(f"/v1/runs/{claimed.id}")
        assert response.status_code == 200
        body = response.json()
        assert 0 < len(body["transcript"]) < 4  # partial: cancelled mid-script

        turn_rows, assertion_rows = await _materialized_rows(engine, claimed.id)
        assert len(turn_rows) == len(body["transcript"])

        assert [_turn_from_row(row) for row in turn_rows] == body["transcript"]
        materialized_assertions = {_assertion_from_row(row)["text"] for row in assertion_rows}
        body_assertions = {a["text"] for a in body["resultAssertions"]}
        assert materialized_assertions == body_assertions
    finally:
        await _cleanup(engine, agent_id)


async def test_materialize_upserts_repeated_assertion_id_last_write_wins() -> None:
    """basic_simulation.json never repeats an assertionId, so the happy-path
    test above can't prove the upsert's last-write-wins semantics agree with
    _reduce_events' dict-keyed (also last-write-wins) reduction -- this
    drives a dedicated fixture script that reports the same assertionId
    twice with different verdicts to actually exercise that path."""
    engine = _test_engine()
    agent_id = await _make_agent(engine)
    await _make_queued_run(engine, agent_id)
    try:
        claimed = await claim_run(engine, "test-worker-materialize-upsert")
        assert claimed is not None
        await run_fake_script(engine, claimed, script_name="assertion_upsert_test.json")

        async with await _client() as client:
            response = await client.get(f"/v1/runs/{claimed.id}")
        assert response.status_code == 200
        body = response.json()
        assert len(body["resultAssertions"]) == 1
        assert body["resultAssertions"][0]["ok"] is True  # last write (passed) won

        turn_rows, assertion_rows = await _materialized_rows(engine, claimed.id)
        assert len(assertion_rows) == 1
        materialized_assertion = _assertion_from_row(assertion_rows[0])
        assert materialized_assertion == body["resultAssertions"][0]
        assert materialized_assertion["ok"] is True

        assert [_turn_from_row(row) for row in turn_rows] == body["transcript"]
    finally:
        await _cleanup(engine, agent_id)
