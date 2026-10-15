"""B4-01: GET /v1/attack-packs -- the packs a red-team run can draw from.

Read-only and global (packs ship with the app, they are not project data).
Returns a summary, not the attack script: the lines an attacker will say are
not something a list screen needs.
"""

from fastapi import APIRouter, Depends

from app.deps import require_user_id
from app.engine.redteam.packs import registry
from app.schemas.attack_packs import AttackPackSummary

router = APIRouter(tags=["attack-packs"])


@router.get("/v1/attack-packs", response_model=list[AttackPackSummary])
async def list_attack_packs(_user_id: str = Depends(require_user_id)) -> list[AttackPackSummary]:
    return [
        AttackPackSummary(
            id=p.id,
            version=p.version,
            name=p.name,
            category=p.category,
            requires_verified=p.requires_verified,
            severity_if_success=p.severity_if_success,
            turns=p.turns,
        )
        for p in registry()
    ]
