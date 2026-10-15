"""B4-05: findings read shapes (defined in app.schemas.runs, which embeds
FindingDetail in RunDetail; re-exported here for the findings API)."""

from app.schemas.runs import APIModel, FindingDetail, FindingSummary


class FindingResolve(APIModel):
    resolved: bool


__all__ = ["FindingDetail", "FindingResolve", "FindingSummary"]
