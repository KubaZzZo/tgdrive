from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException, Request, status

from app.config import Settings
from app.infrastructure.db import Database
from app.infrastructure.storage import TelegramStorage
from app.services.uploads import UploadService


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Database:
    return request.state.telegram_runtime.db


def get_storage(request: Request) -> TelegramStorage:
    return request.state.telegram_runtime.storage


def get_upload_service(request: Request) -> UploadService:
    return request.state.telegram_runtime.uploads


def require_storage(storage: TelegramStorage = Depends(get_storage)) -> TelegramStorage:
    status_info: dict[str, Any] = storage.status()
    if not status_info.get("connected"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=status_info.get("error") or "Telegram 尚未连接",
        )
    return storage
