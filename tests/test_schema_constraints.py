"""Integration tests against the real brain DB (B0-04c): prove the CHECK and
UNIQUE rules from spine §3 actually reject bad rows. Each test runs inside a
rolled-back transaction — nothing persists. Skipped if DATABASE_URL isn't
configured (e.g. a CI job that hasn't been given the secret yet).
"""

import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from tests.conftest import _test_engine, requires_test_db

pytestmark = requires_test_db


@pytest_asyncio.fixture
async def conn() -> AsyncGenerator[AsyncConnection]:
    # NullPool: asyncpg connections are bound to the event loop that created
    # them; pytest-asyncio hands each test a fresh loop, so a pooled/cached
    # engine reused across tests raises "Future attached to a different loop".
    engine = _test_engine()
    async with engine.connect() as connection:
        yield connection
        await connection.rollback()
    await engine.dispose()


async def _expect_integrity_error(
    conn: AsyncConnection, stmt: str, params: dict[str, object]
) -> None:
    with pytest.raises(IntegrityError):
        async with conn.begin_nested():
            await conn.execute(text(stmt), params)


async def _seed_agent(conn: AsyncConnection) -> uuid.UUID:
    agent_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO agents (id, name, transport, created_by_user_id) "
            "VALUES (:id, 'Test Agent', 'web', 'user-1')"
        ),
        {"id": agent_id},
    )
    return agent_id


async def _seed_run(
    conn: AsyncConnection, agent_id: uuid.UUID, run_type: str = "simulation"
) -> uuid.UUID:
    run_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO runs (id, type, agent_id, created_by_user_id) "
            "VALUES (:id, :type, :agent_id, 'user-1')"
        ),
        {"id": run_id, "type": run_type, "agent_id": agent_id},
    )
    return run_id


async def test_findings_evidence_required_unless_blocked(conn: AsyncConnection) -> None:
    agent_id = await _seed_agent(conn)
    run_id = await _seed_run(conn, agent_id, "redteam")

    finding_sql = (
        "INSERT INTO findings "
        "(id, run_id, category, severity, verdict, attack_prompt, agent_response, "
        "suggested_fix, evidence) "
        "VALUES (:id, :run_id, 'pii_leak', 'critical', :verdict, 'prompt', 'response', "
        "'fix', :evidence)"
    )

    # leaked/bypassed with no evidence must be rejected — the CHECK makes
    # "never bare pass/fail" a database guarantee, not a convention.
    await _expect_integrity_error(
        conn,
        finding_sql,
        {"id": uuid.uuid4(), "run_id": run_id, "verdict": "leaked", "evidence": None},
    )

    # blocked with no evidence is fine (nothing was found to quote)
    await conn.execute(
        text(finding_sql),
        {"id": uuid.uuid4(), "run_id": run_id, "verdict": "blocked", "evidence": None},
    )

    # leaked WITH evidence is fine
    await conn.execute(
        text(finding_sql),
        {"id": uuid.uuid4(), "run_id": run_id, "verdict": "leaked", "evidence": "verbatim quote"},
    )


async def test_scenarios_source_draft_ref_unique(conn: AsyncConnection) -> None:
    agent_id = await _seed_agent(conn)
    suite_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO suites (id, name, agent_id, created_by_user_id) "
            "VALUES (:id, 'Test Suite', :agent_id, 'user-1')"
        ),
        {"id": suite_id, "agent_id": agent_id},
    )

    scenario_sql = (
        "INSERT INTO scenarios "
        "(id, suite_id, name, persona, persona_initials, source, source_draft_ref) "
        "VALUES (:id, :suite_id, 'Scenario', 'Persona', 'P', 'discovery_draft', :ref)"
    )
    shared_ref = f"draft-{uuid.uuid4()}"

    await conn.execute(
        text(scenario_sql), {"id": uuid.uuid4(), "suite_id": suite_id, "ref": shared_ref}
    )
    # idempotent "Add to suite": a second scenario reusing the same draft ref
    # must be rejected at the DB level — the API turns this into a 200 replay.
    await _expect_integrity_error(
        conn, scenario_sql, {"id": uuid.uuid4(), "suite_id": suite_id, "ref": shared_ref}
    )


async def test_runs_idempotency_key_unique(conn: AsyncConnection) -> None:
    agent_id = await _seed_agent(conn)
    run_sql = (
        "INSERT INTO runs (id, type, agent_id, created_by_user_id, idempotency_key) "
        "VALUES (:id, 'simulation', :agent_id, 'user-1', :key)"
    )
    shared_key = f"idem-{uuid.uuid4()}"

    await conn.execute(text(run_sql), {"id": uuid.uuid4(), "agent_id": agent_id, "key": shared_key})
    await _expect_integrity_error(
        conn, run_sql, {"id": uuid.uuid4(), "agent_id": agent_id, "key": shared_key}
    )


async def test_discovery_node_blocked_reason_required(conn: AsyncConnection) -> None:
    agent_id = await _seed_agent(conn)
    run_id = await _seed_run(conn, agent_id, "discovery")

    node_sql = (
        "INSERT INTO discovery_nodes (run_id, node_id, label, x, y, state, blocked_reason) "
        "VALUES (:run_id, :node_id, 'Node', 0, 0, :state, :reason)"
    )

    # the identity-gate explanation cannot be silently dropped
    await _expect_integrity_error(
        conn, node_sql, {"run_id": run_id, "node_id": "n1", "state": "blocked", "reason": None}
    )

    await conn.execute(
        text(node_sql),
        {"run_id": run_id, "node_id": "n2", "state": "blocked", "reason": "wrong DOB"},
    )
    await conn.execute(
        text(node_sql),
        {"run_id": run_id, "node_id": "n3", "state": "mapped", "reason": None},
    )


async def test_runs_end_reason_valid(conn: AsyncConnection) -> None:
    """B2-06: end_reason is nullable (CHECK passes NULL through unchecked --
    "null is acceptable until the executor writes it") but any non-null
    value must be one of the enumerated reasons."""
    agent_id = await _seed_agent(conn)
    run_sql = (
        "INSERT INTO runs (id, type, agent_id, created_by_user_id, end_reason) "
        "VALUES (:id, 'simulation', :agent_id, 'user-1', :end_reason)"
    )

    await _expect_integrity_error(
        conn, run_sql, {"id": uuid.uuid4(), "agent_id": agent_id, "end_reason": "not_a_reason"}
    )

    await conn.execute(
        text(run_sql), {"id": uuid.uuid4(), "agent_id": agent_id, "end_reason": None}
    )
    await conn.execute(
        text(run_sql), {"id": uuid.uuid4(), "agent_id": agent_id, "end_reason": "timeout"}
    )


async def test_discovery_intent_reason_required(conn: AsyncConnection) -> None:
    agent_id = await _seed_agent(conn)
    run_id = await _seed_run(conn, agent_id, "discovery")

    intent_sql = (
        "INSERT INTO discovery_intents (run_id, name, state, path, reason) "
        "VALUES (:run_id, :name, :state, 'path', :reason)"
    )

    await _expect_integrity_error(
        conn,
        intent_sql,
        {"run_id": run_id, "name": "card_block", "state": "blocked", "reason": None},
    )

    await conn.execute(
        text(intent_sql),
        {"run_id": run_id, "name": "card_block", "state": "blocked", "reason": "wrong phrase"},
    )
