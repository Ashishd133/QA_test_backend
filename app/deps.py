from fastapi import Request, status

from app.errors import APIError


def require_user_id(request: Request) -> str:
    """FastAPI dependency for write endpoints — fail loudly instead of creating
    ownerless rows when the BFF forgets to forward X-User-Id (spine §0)."""
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise APIError(
            code="missing_user_id",
            message="X-User-Id header is required for this endpoint",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return str(user_id)
