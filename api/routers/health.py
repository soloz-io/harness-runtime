from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/healthz/ready")
async def healthz_ready() -> dict[str, str]:
    return {"status": "ok"}
