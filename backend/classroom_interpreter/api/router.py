from fastapi import APIRouter

from .health import router as health_router
from .sessions import router as sessions_router
from .websocket import router as websocket_router

router = APIRouter()
router.include_router(health_router)
router.include_router(sessions_router)
router.include_router(websocket_router)
