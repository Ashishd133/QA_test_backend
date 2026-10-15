"""B4-04/B4-05: the findings write path.

`evidence` and `agent_response` are the verbatim text an agent leaked --
potentially real customer data -- so they are encrypted at rest (same scheme
and keys as test profiles, `app.crypto.encrypt_text`); everything else on a
finding (category, severity, verdict, the attacker's own prompt, the fix) is
not sensitive and stays queryable. The `findings` CHECK constraint
(verdict = 'blocked' OR evidence IS NOT NULL) is the DB-level backstop for
"a leak without evidence cannot exist".
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from app.crypto import decrypt_text, encrypt_text
from app.engine.redteam.judge import GradedAttack
from app.engine.redteam.packs import AttackPack
from app.schemas.findings import FindingDetail, FindingSummary

_INSERT_FINDING_SQL = text(
    "INSERT INTO findings "
    "(id, run_id, category, severity, verdict, attack_prompt, agent_response, evidence, "
    " suggested_fix, turn_refs) "
    "VALUES (:id, :run_id, :category, :severity, :verdict, :attack_prompt, :agent_response, "
    " :evidence, :suggested_fix, :turn_refs)"
)


async def insert_finding(
    conn: AsyncConnection,
    *,
    run_id: uuid.UUID,
    pack: AttackPack,
    graded: GradedAttack,
    attack_prompt: str,
    agent_response: str,
) -> uuid.UUID:
    finding_id = uuid.uuid4()
    await conn.execute(
        _INSERT_FINDING_SQL,
        {
            "id": finding_id,
            "run_id": run_id,
            "category": pack.category,
            "severity": graded.severity,
            "verdict": graded.verdict,
            "attack_prompt": attack_prompt,
            "agent_response": encrypt_text(agent_response),
            "evidence": encrypt_text(graded.evidence) if graded.evidence is not None else None,
            "suggested_fix": graded.suggested_fix,
            "turn_refs": graded.turn_refs,
        },
    )
    return finding_id


FINDING_COLUMNS = (
    "f.id, f.run_id, f.category, f.severity, f.verdict, f.attack_prompt, f.agent_response, "
    "f.evidence, f.suggested_fix, f.turn_refs, f.resolved_at, f.created_at"
)

_LOG_READ_SQL = text(
    "INSERT INTO sensitive_access_log (id, resource_type, resource_id, user_id, action) "
    "VALUES (:id, 'finding', :resource_id, :user_id, 'read')"
)


def finding_summary(row: RowMapping) -> FindingSummary:
    return FindingSummary(
        id=str(row["id"]),
        run_id=str(row["run_id"]),
        category=row["category"],
        severity=row["severity"],
        verdict=row["verdict"],
        attack_prompt=row["attack_prompt"],
        suggested_fix=row["suggested_fix"],
        resolved_at=row["resolved_at"].isoformat() if row["resolved_at"] is not None else None,
        created_at=row["created_at"].isoformat(),
    )


async def reveal_findings(
    conn: AsyncConnection, rows: list[RowMapping], *, user_id: str
) -> list[FindingDetail]:
    """Decrypt findings for a caller and record that they did, in the same
    transaction as the read (the audit row cannot be skipped by a crash
    between the two). One log row per finding revealed. Legacy plaintext
    rows (pre-B4-05 seed data) pass through `decrypt_text` unchanged."""
    out: list[FindingDetail] = []
    for row in rows:
        await conn.execute(
            _LOG_READ_SQL, {"id": uuid.uuid4(), "resource_id": row["id"], "user_id": user_id}
        )
        summary = finding_summary(row)
        out.append(
            FindingDetail(
                **summary.model_dump(),
                agent_response=decrypt_text(row["agent_response"]),
                evidence=decrypt_text(row["evidence"]) if row["evidence"] is not None else None,
                turn_refs=list(row["turn_refs"] or []),
            )
        )
    return out
