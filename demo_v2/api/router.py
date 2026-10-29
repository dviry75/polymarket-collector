from fastapi import APIRouter, Depends, Request

from ..health import HealthSnapshot
from .dependencies import require_demo_session


router = APIRouter(prefix="/api", dependencies=[Depends(require_demo_session)])


@router.get("/health", response_model=HealthSnapshot)
async def health(request: Request) -> HealthSnapshot:
    return request.app.state.health.snapshot()
