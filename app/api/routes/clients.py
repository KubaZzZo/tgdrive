from __future__ import annotations

import asyncio
import hashlib
import re
import sqlite3
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
from app.api.request_body import bounded_body
from app.api.routes.files import FileStreamingResponse
from app.core.security import require_auth
from app.infrastructure.db import Database
from app.infrastructure.db.repositories.index_restore import RestoreChanged
from app.infrastructure.storage import TelegramStorage
from app.services.index_backup import build_index_backup
from app.services.index_restore import MAX_ARCHIVE, read_index_archive

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


@router.post("/index-restore/{stage}")
async def index_restore(
    stage: Literal["preview", "confirm"],
    request: Request,
    include_links: bool = False,
) -> dict:
    runtime = request.state.telegram_runtime
    manager = request.app.state.accounts
    chat_id = require_channel(runtime.storage)
    expected = request.headers.get("x-restore-preview") if stage == "confirm" else None
    if stage == "confirm" and (expected is None or not re.fullmatch(r"[0-9a-f]{64}", expected)):
        raise HTTPException(400, "请预览备份后再确认恢复")
    lock: asyncio.Lock = request.app.state.index_backup_lock
    if lock.locked():
        raise HTTPException(429, "已有频道索引正在处理，请稍后重试")
    async with lock:
        try:
            with TemporaryDirectory(prefix="tgdrive-restore-") as scratch:
                directory = Path(scratch).resolve()
                archive = directory / "index.zip"
                digest = hashlib.sha256()
                async with await open_file(archive, "wb") as output:
                    async for chunk in bounded_body(request, MAX_ARCHIVE):
                        digest.update(chunk)
                        await output.write(chunk)
                with CancelScope(shield=True):
                    manifest, source = await to_thread.run_sync(
                        read_index_archive, archive, directory, chat_id
                    )
                async with manager.lock:
                    if manager.current is not runtime:
                        raise HTTPException(409, "频道已切换，请重新预览")
                    with CancelScope(shield=True):
                        result = await to_thread.run_sync(
                            runtime.db.restore_channel_index,
                            source,
                            chat_id,
                            digest.hexdigest(),
                            include_links,
                            expected,
                        )
                    if stage == "confirm" and any(result["added"].values()):
                        runtime.sync.revision += 1
        except RestoreChanged as exc:
            raise HTTPException(409, str(exc)) from None
        except (ValueError, sqlite3.IntegrityError) as exc:
            detail = str(exc) if isinstance(exc, ValueError) else "备份记录不符合当前数据库约束"
            raise HTTPException(422, detail) from None
        except OSError:
            raise HTTPException(507, "无法写入备份临时文件，请检查磁盘空间") from None
    return {**result, "backup": manifest, "channel_id": chat_id}
