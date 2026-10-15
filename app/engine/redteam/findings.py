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
from sqlalchemy.ext.asyncio import AsyncConnection

from app.crypto import encrypt_text
from app.engine.redteam.judge import GradedAttack
from app.engine.redteam.packs import AttackPack

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
