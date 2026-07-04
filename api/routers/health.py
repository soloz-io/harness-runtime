from fastapi import APIRouter
from fastapi.responses import JSONResponse

from api.registry import is_registration_complete

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/healthz/ready")
async def healthz_ready() -> JSONResponse:
    if is_registration_complete():
        return JSONResponse({"status": "ok"})
    return JSONResponse({"status": "not_ready"}, status_code=503)
