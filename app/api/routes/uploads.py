from __future__ import annotations

import asyncio
import errno
import secrets
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.requests import ClientDisconnect

from app.api.deps import get_db, get_settings, get_upload_service, require_storage
from app.api.schemas.uploads import UploadCreateBody
from app.config import Settings
from app.core.security import require_auth
from app.infrastructure.db import Database
from app.infrastructure.storage import TelegramStorage
from app.services.image_host import MAX_IMAGE_SIZE
from app.services.uploads import UploadService, UploadSpaceError

router = APIRouter(prefix="/api/uploads", tags=["uploads"], dependencies=[Depends(require_auth)])


def public_upload(upload: dict[str, Any]) -> dict[str, Any]:
    """Never expose server-local temp paths to the browser."""
    return {key: value for key, value in upload.items() if key != "temp_path"}


def is_disk_full(exc: OSError) -> bool:
    return exc.errno in {errno.ENOSPC, errno.EDQUOT} or getattr(exc, "winerror", None) in {39, 112}


@router.post("")
async def create_upload(
    body: UploadCreateBody,
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_settings),
    storage: TelegramStorage = Depends(require_storage),
    uploads: UploadService = Depends(get_upload_service),
) -> dict[str, Any]:
    if body.image_host and not uploads.image_host_enabled:
        raise HTTPException(404, "图床功能已关闭")
    if body.image_host and body.size > MAX_IMAGE_SIZE:
        raise HTTPException(413, "图床单张图片不能超过 20 MiB")
    if settings.max_file_size and body.size > settings.max_file_size:
        raise HTTPException(413, "文件超过配置的最大大小")
    temp_path = settings.temp_dir / f"upload_{secrets.token_hex(16)}.part"
    try:
        await uploads.space.check_admission(settings.temp_dir, body.size)
        if uploads._closed:
            raise HTTPException(409, "账号正在切换，请刷新页面后重试")
        if body.parent_id:
            parent = db.get_item(body.parent_id)
            if not parent:
                raise HTTPException(404, "目标文件夹不存在")
            if parent["kind"] != "folder":
                raise HTTPException(400, "目标不是文件夹")
        upload = db.create_upload(
            parent_id=body.parent_id,
            name=body.name,
            size=body.size,
            mime=body.mime,
            temp_path=str(temp_path),
            chunk_size=settings.upload_chunk_size,
            image_host=body.image_host,
        )
        try:
            temp_path.touch(exist_ok=False)
        except OSError:
            db.delete_upload(upload["id"])
            raise
    except Exception as exc:
        if isinstance(exc, ValueError):
            raise HTTPException(400, str(exc)) from None
        if isinstance(exc, UploadSpaceError):
            raise HTTPException(507, str(exc)) from None
        if isinstance(exc, OSError) and is_disk_full(exc):
            raise HTTPException(507, "上传磁盘空间不足，请释放空间后重试") from None
        raise
    return {
        "id": upload["id"],
        "offset": 0,
        "chunk_size": upload["chunk_size"],
        "size": upload["size"],
    }


@router.get("")
async def list_uploads(
    db: Database = Depends(get_db), uploads: UploadService = Depends(get_upload_service)
) -> dict[str, Any]:
    """列出未完成的上传任务。"""
    return {
        "uploads": [
            public_upload(item)
            for item in db.list_uploads()
            if uploads.image_host_enabled or not item["image_host"]
        ]
    }


@router.get("/{upload_id}")
async def get_upload(
    upload_id: str,
    db: Database = Depends(get_db),
    uploads: UploadService = Depends(get_upload_service),
) -> dict[str, Any]:
    upload = db.get_upload(upload_id)
    if (
        not upload
        or upload["link_import"]
        or (upload["image_host"] and not uploads.image_host_enabled)
    ):
        raise HTTPException(404, "上传任务不存在")
    return public_upload(upload)


@router.patch("/{upload_id}", response_model=None)
async def upload_chunk(
    upload_id: str,
    request: Request,
    db: Database = Depends(get_db),
    uploads: UploadService = Depends(get_upload_service),
) -> dict[str, Any] | JSONResponse:
    async with uploads.lock(upload_id):
        upload = db.get_upload(upload_id)
        if not upload or upload["link_import"]:
            raise HTTPException(404, "上传任务不存在")
        if upload["image_host"] and not uploads.image_host_enabled:
            raise HTTPException(404, "图床功能已关闭")
        if upload["state"] not in {"uploading", "error"}:
            raise HTTPException(409, f"当前状态不能继续上传: {upload['state']}")
        try:
            offset = int(request.headers.get("upload-offset", ""))
        except ValueError:
            raise HTTPException(400, "缺少或非法 Upload-Offset") from None
        if offset < 0:
            raise HTTPException(400, "Upload-Offset 不能为负数")
        received = int(upload["received"])
        if offset < received:
            # 客户端重发已写入的分片（上次响应丢失），幂等返回当前进度
            return {
                "offset": received,
                "received": received,
                "size": upload["size"],
                "duplicate": True,
            }
        if offset > received:
            return JSONResponse({"error": "offset 不匹配", "offset": received}, status_code=409)

        remaining = int(upload["size"]) - offset
        if remaining <= 0:
            raise HTTPException(400, "文件已上传完成，请调用 complete")
        chunk_limit = min(int(upload["chunk_size"]), remaining)

        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                raise HTTPException(400, "Content-Length 无效") from None
            if declared <= 0:
                raise HTTPException(400, "空分片")
            if declared > chunk_limit:
                raise HTTPException(413, "分片超过约定大小")

        written = 0
        path = Path(upload["temp_path"])
        try:
            with path.open("r+b", buffering=0) as fh:
                if fh.seek(0, 2) < offset:
                    raise HTTPException(409, "临时文件不完整，请重新上传")
                fh.truncate(offset)
                fh.seek(offset)
                stream = request.stream()
                while True:
                    async with asyncio.timeout(60):
                        chunk = await anext(stream, None)
                    if chunk is None:
                        break
                    if not chunk:
                        continue
                    if written + len(chunk) > chunk_limit:
                        raise HTTPException(413, "分片超过约定大小")
                    await uploads.space.write(path.parent, fh, chunk)
                    written += len(chunk)
                if written == 0:
                    raise HTTPException(400, "空分片")
                if content_length is not None and written != declared:
                    raise HTTPException(400, "分片长度与 Content-Length 不匹配")
                new_offset = offset + written
                db.update_upload(upload_id, received=new_offset, state="uploading", error=None)
        except BaseException as exc:
            uploads.discard_receiving(upload)
            if isinstance(exc, ClientDisconnect):
                raise HTTPException(400, "上传连接已断开，请重新上传") from None
            if isinstance(exc, TimeoutError):
                raise HTTPException(408, "上传超时，请重新上传") from None
            if isinstance(exc, FileNotFoundError):
                raise HTTPException(404, "临时文件不存在，请重新上传") from None
            if isinstance(exc, UploadSpaceError):
                raise HTTPException(507, str(exc)) from None
            if isinstance(exc, OSError) and is_disk_full(exc):
                raise HTTPException(507, "上传磁盘空间不足，请释放空间后重试") from None
            raise
        return {"offset": new_offset, "received": new_offset, "size": upload["size"]}


@router.delete("/{upload_id}")
async def cancel_upload(
    upload_id: str,
    db: Database = Depends(get_db),
    uploads: UploadService = Depends(get_upload_service),
) -> dict[str, bool]:
    async with uploads.lock(upload_id):
        upload = db.get_upload(upload_id)
        if not upload or upload["link_import"]:
            raise HTTPException(404, "上传任务不存在")
        if upload["state"] == "processing":
            raise HTTPException(409, "正在写入 Telegram，无法取消")
        if upload["state"] != "done":
            uploads.discard_upload(upload)
        return {"ok": True}


@router.post("/{upload_id}/complete", response_model=None)
async def complete_upload(
    upload_id: str,
    db: Database = Depends(get_db),
    uploads: UploadService = Depends(get_upload_service),
    storage: TelegramStorage = Depends(require_storage),
) -> dict[str, Any] | JSONResponse:
    async with uploads.lock(upload_id):
        del storage  # dependency guarantees connectivity before scheduling work
        if uploads._closed:
            raise HTTPException(409, "账号正在切换，请刷新页面后重试")
        upload = db.get_upload(upload_id)
        if not upload or upload["link_import"]:
            raise HTTPException(404, "上传任务不存在")
        if upload["image_host"] and not uploads.image_host_enabled:
            raise HTTPException(404, "图床功能已关闭")
        if upload["state"] == "done":
            return public_upload(upload)
        if upload["state"] not in {"uploading", "error"}:
            return JSONResponse(
                {"error": "正在处理中", "upload": public_upload(upload)}, status_code=202
            )
        if int(upload["received"]) != int(upload["size"]):
            raise HTTPException(400, f"文件尚未上传完成: {upload['received']}/{upload['size']}")
        db.update_upload(upload_id, state="processing", progress=0, error=None)
        if not uploads.schedule(upload_id):
            return JSONResponse(
                {
                    "error": "任务已在处理中",
                    "upload": public_upload(db.get_upload(upload_id) or upload),
                },
                status_code=202,
            )
        return JSONResponse(
            {"ok": True, "upload": public_upload(db.get_upload(upload_id) or upload)},
            status_code=202,
        )
