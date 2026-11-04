from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import get_db, get_settings, get_storage
from app.auth import is_authenticated, require_auth
from app.config import Settings
from app.db import Database
from app.storage import TelegramStorage

router = APIRouter(prefix="/api", tags=["system"])


class TrafficSettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quota_bytes: int = Field(ge=1, le=10**16)


@router.get("/traffic", dependencies=[Depends(require_auth)])
async def traffic_status(request: Request) -> dict:
    return request.app.state.traffic.snapshot()


@router.put("/traffic/settings", dependencies=[Depends(require_auth)])
async def traffic_settings(body: TrafficSettingsBody, request: Request) -> dict:
    request.app.state.db.update_traffic_config(body.quota_bytes)
    return request.app.state.traffic.snapshot()


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


class SyncBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    full: bool = False


@router.get("/sync", dependencies=[Depends(require_auth)])
async def sync_status(request: Request) -> dict:
    return request.app.state.channel_sync.status()


@router.post("/sync", dependencies=[Depends(require_auth)], status_code=202)
async def request_sync(body: SyncBody, request: Request) -> dict:
    service = request.app.state.channel_sync
    if not service.settings.channel_sync_enabled:
        raise HTTPException(409, "频道同步已关闭")
    if service.state in {"unavailable", "stopped"}:
        raise HTTPException(503, "Telegram 同步服务尚未就绪")
    service.request_scan(body.full)
    return service.status()
