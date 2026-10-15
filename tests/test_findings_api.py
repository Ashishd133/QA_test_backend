"""B4-05: the findings read path -- masked list, access-logged reveal, run
detail, resolve, and tolerance for pre-encryption plaintext rows."""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.crypto import encrypt_text
from app.engine.redteam.findings import insert_finding
from app.engine.redteam.judge import GradedAttack
from app.engine.redteam.packs import load_pack_file
from app.main import app
from tests.conftest import _test_engine, auth_headers, requires_test_db

pytestmark = [requires_test_db, pytest.mark.timeout(600)]

EVIDENCE = "card ending 3342 and a phone on file ending 80"
RESPONSE = "Vikram Nair's account ending 9215 has a card ending 3342 and a phone on file ending 80."


async def _client(user_id: str | None = "user-1") -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=auth_headers(user_id=user_id),
    )


async def _setup(engine: AsyncEngine) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """(agent, red-team child run, leaked finding, blocked finding)."""
    from app.engine.redteam.packs import PACKS_DIR

    pack = load_pack_file(PACKS_DIR / "pii-family-pretext.yaml")
    agent_id, parent_id, child_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO agents (id, name, transport, created_by_user_id) "
                "VALUES (:id, 'Findings Agent', 'web', 'user-1')"
            ),
            {"id": agent_id},
        )
        await conn.execute(
            text(
                "INSERT INTO runs (id, type, status, agent_id, created_by_user_id) "
                "VALUES (:id, 'redteam', 'completed', :a, 'user-1')"
            ),
            {"id": parent_id, "a": agent_id},
        )
        await conn.execute(
            text(
                "INSERT INTO runs (id, type, status, agent_id, parent_run_id, created_by_user_id) "
                "VALUES (:id, 'redteam', 'completed', :a, :p, 'user-1')"
            ),
            {"id": child_id, "a": agent_id, "p": parent_id},
        )
        leaked = await insert_finding(
            conn,
            run_id=child_id,
            pack=pack,
            graded=GradedAttack(
                "leaked",
                "critical",
                EVIDENCE,
                [3],
                "r",
                "Require the other customer's verification.",
            ),
            attack_prompt="about my husband",
            agent_response=RESPONSE,
        )
        blocked = await insert_finding(
            conn,
            run_id=child_id,
            pack=pack,
            graded=GradedAttack("blocked", "low", None, [], "held", "No change needed."),
            attack_prompt="again",
            agent_response="I can't help with that.",
        )
    return agent_id, child_id, leaked, blocked


async def _cleanup(engine: AsyncEngine, agent_id: uuid.UUID) -> None:
    async with engine.connect() as conn, conn.begin():
        ids = (
            (await conn.execute(text("SELECT id FROM runs WHERE agent_id = :a"), {"a": agent_id}))
            .scalars()
            .all()
        )
        fids = (
            (
                await conn.execute(
                    text("SELECT id FROM findings WHERE run_id = ANY(:ids)"), {"ids": ids}
                )
            )
            .scalars()
            .all()
        )
        await conn.execute(
            text("DELETE FROM sensitive_access_log WHERE resource_id = ANY(:ids)"), {"ids": fids}
        )
        await conn.execute(text("DELETE FROM findings WHERE run_id = ANY(:ids)"), {"ids": ids})
        await conn.execute(text("DELETE FROM runs WHERE agent_id = :a"), {"a": agent_id})
        await conn.execute(text("DELETE FROM agents WHERE id = :a"), {"a": agent_id})
    await engine.dispose()


async def _log_count(engine: AsyncEngine, finding_id: uuid.UUID) -> int:
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM sensitive_access_log "
                        "WHERE resource_type = 'finding' AND resource_id = :id"
                    ),
                    {"id": finding_id},
                )
            ).scalar_one()
        )


async def test_masked_list_logged_reveal_run_detail_and_resolve() -> None:
    engine = _test_engine()
    agent_id, child_id, leaked, blocked = await _setup(engine)
    try:
        async with await _client() as client:
            listed = await client.get("/v1/findings", params={"severity": "critical"})
            mine = [f for f in listed.json() if f["runId"] == str(child_id)]
            # Masked: no leaked text anywhere in the list row.
            assert len(mine) == 1 and mine[0]["id"] == str(leaked)
            assert "evidence" not in mine[0] and "agentResponse" not in mine[0]
            assert EVIDENCE not in listed.text
            assert mine[0]["verdict"] == "leaked" and mine[0]["resolvedAt"] is None
            assert await _log_count(engine, leaked) == 0  # listing reveals nothing

            # Blocked attacks are hidden unless asked for.
            only_blocked = await client.get("/v1/findings", params={"verdict": "blocked"})
            assert str(blocked) in {f["id"] for f in only_blocked.json()}
            assert str(blocked) not in {f["id"] for f in listed.json()}

            # Reveal: plaintext, and the read is logged.
            detail = await client.get(f"/v1/findings/{leaked}")
            body = detail.json()
            assert body["evidence"] == EVIDENCE and body["agentResponse"] == RESPONSE
            assert body["turnRefs"] == [3] and "verification" in body["suggestedFix"]
            assert await _log_count(engine, leaked) == 1

            # The call's own detail carries its findings, also logged.
            run = (await client.get(f"/v1/runs/{child_id}")).json()
            by_id = {f["id"]: f for f in run["findings"]}
            assert by_id[str(leaked)]["evidence"] == EVIDENCE
            assert by_id[str(blocked)]["evidence"] is None
            assert await _log_count(engine, leaked) == 2

            # Resolve / reopen.
            done = await client.patch(f"/v1/findings/{leaked}", json={"resolved": True})
            assert done.json()["resolvedAt"] is not None
            open_now = await client.get("/v1/findings", params={"resolved": "false"})
            assert str(leaked) not in {f["id"] for f in open_now.json()}
            reopened = await client.patch(f"/v1/findings/{leaked}", json={"resolved": False})
            assert reopened.json()["resolvedAt"] is None

            missing = await client.get(f"/v1/findings/{uuid.uuid4()}")
            assert missing.status_code == 404
    finally:
        await _cleanup(engine, agent_id)


async def test_ciphertext_at_rest_legacy_plaintext_tolerated_and_attribution_required() -> None:
    engine = _test_engine()
    agent_id, child_id, leaked, _blocked = await _setup(engine)
    try:
        async with engine.connect() as conn:
            raw = (
                await conn.execute(
                    text("SELECT evidence, agent_response FROM findings WHERE id = :id"),
                    {"id": leaked},
                )
            ).one()
        assert "3342" not in raw[0] and "3342" not in raw[1]

        # A pre-B4-05 row (plaintext columns) still reads.
        legacy = uuid.uuid4()
        async with engine.connect() as conn, conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO findings (id, run_id, category, severity, verdict, attack_prompt,"
                    " agent_response, evidence, suggested_fix) VALUES (:id, :r, 'PII', 'critical',"
                    " 'leaked', 'p', 'old plaintext response', 'old plaintext evidence', 'fix')"
                ),
                {"id": legacy, "r": child_id},
            )
        async with await _client() as client:
            body = (await client.get(f"/v1/findings/{legacy}")).json()
        assert body["evidence"] == "old plaintext evidence"
        assert body["agentResponse"] == "old plaintext response"

        # No X-User-Id -> no plaintext (nothing to attribute the read to).
        async with await _client(user_id=None) as client:
            assert (await client.get(f"/v1/findings/{leaked}")).status_code == 400
            assert (await client.get(f"/v1/runs/{child_id}")).status_code == 400
            # Every project-scoped read already requires a caller (deps.
            # require_project_id), so the masked list is attributable too.
            assert (await client.get("/v1/findings")).status_code == 400
    finally:
        await _cleanup(engine, agent_id)


def test_encrypt_text_is_not_plaintext_and_round_trips() -> None:
    from app.crypto import decrypt_text

    token = encrypt_text("secret leak")
    assert "secret" not in token
    assert decrypt_text(token) == "secret leak"
    assert decrypt_text("never encrypted") == "never encrypted"
    assert decrypt_text('{"v": 1}') == '{"v": 1}'  # JSON-shaped but not our envelope
