from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from app.api.deps import get_db, get_settings, get_storage
from app.api.schemas.system import SyncBody, TrafficSettingsBody
from app.config import Settings
from app.core.security import is_authenticated, require_auth
from app.infrastructure.db import Database
from app.infrastructure.storage import TelegramStorage

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/traffic", dependencies=[Depends(require_auth)])
async def traffic_status(request: Request) -> dict:
    return {
        **request.app.state.traffic.snapshot(),
        "media_cache": request.app.state.media_cache.snapshot(),
    }


@router.put("/traffic/settings", dependencies=[Depends(require_auth)])
async def traffic_settings(body: TrafficSettingsBody, request: Request) -> dict:
    request.app.state.traffic.db.update_traffic_config(body.quota_bytes)
    return request.app.state.traffic.snapshot()


@router.get("/meta")
async def meta(
    request: Request,
    settings: Settings = Depends(get_settings),
    storage: TelegramStorage = Depends(get_storage),
) -> dict:
    return {
        "app_name": settings.app_name,
        "version": request.app.version,
        "authenticated": is_authenticated(request),
        "storage": storage.status(),
        "chunk_size": settings.upload_chunk_size,
        "max_file_size": settings.max_file_size,
        "workspace_is_default": request.state.telegram_runtime.db
        is request.app.state.accounts.control,
    }


@router.get("/health/live")
async def health_live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def health_ready(
    request: Request,
    db: Database = Depends(get_db),
    storage: TelegramStorage = Depends(get_storage),
) -> JSONResponse:
    db_ok = db.ping()
    storage_status = storage.status()
    bot_ready = getattr(request.state.telegram_runtime.sync, "bot_state", None) == "listening"
    telegram = "ok" if storage_status.get("connected") else "bot_fallback" if bot_ready else "error"
    ready = db_ok and telegram != "error"
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ready" if ready else "degraded",
            "checks": {
                "database": "ok" if db_ok else "error",
                "telegram": telegram,
            },
            "storage_error": storage_status.get("error"),
        },
    )


@router.get("/sync", dependencies=[Depends(require_auth)])
async def sync_status(request: Request) -> dict:
    return request.state.telegram_runtime.sync.status()


@router.post("/sync", dependencies=[Depends(require_auth)], status_code=202)
async def request_sync(body: SyncBody, request: Request) -> dict:
    service = request.state.telegram_runtime.sync
    if not service.settings.channel_sync_enabled:
        raise HTTPException(409, "频道同步已关闭")
    if service.bot_state:
        raise HTTPException(409, "备用 Bot 会自动收录新频道消息，历史扫描需要个人会话。")
    if service.state == "auth_required":
        raise HTTPException(409, service.last_error)
    if service.state in {"unavailable", "stopped"}:
        raise HTTPException(503, "Telegram 同步服务尚未就绪")
    service.request_scan(body.full)
    return service.status()
