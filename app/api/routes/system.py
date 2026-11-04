from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.api.deps import get_db, get_settings, get_storage
from app.auth import is_authenticated
from app.config import Settings
from app.db import Database
from app.storage import TelegramStorage

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/meta")
async def meta(
    request: Request,
    settings: Settings = Depends(get_settings),
    storage: TelegramStorage = Depends(get_storage),
) -> dict:
    return {
        "app_name": settings.app_name,
        "authenticated": is_authenticated(request),
        "storage": storage.status(),
        "chunk_size": settings.upload_chunk_size,
        "max_file_size": settings.max_file_size,
    }


@router.get("/health/live")
async def health_live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def health_ready(
    db: Database = Depends(get_db),
    storage: TelegramStorage = Depends(get_storage),
) -> JSONResponse:
    db_ok = db.ping()
    storage_status = storage.status()
    ready = db_ok and bool(storage_status.get("connected"))
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ready" if ready else "degraded",
            "checks": {
                "database": "ok" if db_ok else "error",
                "telegram": "ok" if storage_status.get("connected") else "error",
            },
            "storage_error": storage_status.get("error"),
        },
    )
