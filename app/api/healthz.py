from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/v1/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
