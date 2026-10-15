from app.engine.redteam.packs import Severity
from app.schemas.runs import APIModel, AttackCategoryKey


class AttackPackSummary(APIModel):
    id: str
    version: int
    name: str
    category: AttackCategoryKey
    requires_verified: bool
    severity_if_success: Severity
    turns: int
