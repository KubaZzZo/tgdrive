from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Literal

from anyio import CancelScope, open_file, to_thread
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, StringConstraints
from starlette.types import Receive, Scope, Send

from app.api.client_auth import require_channel
from app.api.deps import get_db, get_storage
from app.api.routes.files import FileStreamingResponse
from app.core.security import require_auth
from app.infrastructure.db import Database
from app.infrastructure.storage import TelegramStorage
from app.services.index_backup import build_index_backup

router = APIRouter(prefix="/api/clients", tags=["clients"], dependencies=[Depends(require_auth)])


class CredentialBody(BaseModel):
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    scope: Literal["picgo", "webdav"]


@router.get("/credentials")
async def credentials(
    db: Database = Depends(get_db), storage: TelegramStorage = Depends(get_storage)
) -> dict:
    chat_id = require_channel(storage)
    return {"channel_id": chat_id, "credentials": db.list_client_credentials(chat_id)}


@router.post("/credentials", status_code=201)
async def add_credential(
    body: CredentialBody,
    request: Request,
    db: Database = Depends(get_db),
    storage: TelegramStorage = Depends(get_storage),
) -> dict:
    if body.scope == "picgo" and not request.app.state.settings.image_host_enabled:
        raise HTTPException(404, "图床功能已关闭")
    return db.create_client_credential(require_channel(storage), body.label, body.scope)


@router.delete("/credentials/{credential_id}")
async def revoke_credential(
    credential_id: str,
    db: Database = Depends(get_db),
    storage: TelegramStorage = Depends(get_storage),
) -> dict:
    if not db.revoke_client_credential(require_channel(storage), credential_id):
        raise HTTPException(404, "凭据不存在")
    return {"ok": True}


class IndexBackupResponse(FileStreamingResponse):
    def __init__(self, path: Path, filename: str, scratch: TemporaryDirectory, lock: asyncio.Lock):
        async def chunks() -> AsyncIterator[bytes]:
            async with await open_file(path, "rb") as archive:
                while chunk := await archive.read(64 * 1024):
                    yield chunk

        super().__init__(
            chunks(),
            media_type="application/zip",
            headers={
                "Cache-Control": "private, no-store",
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(path.stat().st_size),
            },
        )
        self.scratch = scratch
        self.lock = lock

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                self.scratch.cleanup()
            finally:
                self.lock.release()


@router.get("/index-backup", response_class=StreamingResponse)
async def index_backup(
    request: Request,
    db: Database = Depends(get_db),
    storage: TelegramStorage = Depends(get_storage),
) -> StreamingResponse:
    chat_id = require_channel(storage)
    lock: asyncio.Lock = request.app.state.index_backup_lock
    if lock.locked():
        raise HTTPException(429, "已有频道索引正在导出，请稍后重试")
    await lock.acquire()
    scratch = None
    try:
        scratch = TemporaryDirectory(prefix="tgdrive-index-")
        with CancelScope(shield=True):
            path = await to_thread.run_sync(
                build_index_backup, db, Path(scratch.name), chat_id, request.app.version
            )
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return IndexBackupResponse(path, f"tgdrive-index-{timestamp}.zip", scratch, lock)
    except BaseException:
        try:
            if scratch is not None:
                scratch.cleanup()
        finally:
            lock.release()
        raise
