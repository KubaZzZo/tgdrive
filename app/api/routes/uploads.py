from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

import aiofiles
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from app.api.deps import get_db, get_settings, get_upload_service, require_storage
from app.auth import require_auth
from app.config import Settings
from app.db import Database
from app.schemas import UploadCreateBody
from app.services.uploads import UploadService
from app.storage import TelegramStorage

router = APIRouter(prefix="/api/uploads", tags=["uploads"], dependencies=[Depends(require_auth)])


def public_upload(upload: dict[str, Any]) -> dict[str, Any]:
    """Never expose server-local temp paths to the browser."""
    return {key: value for key, value in upload.items() if key != "temp_path"}


@router.post("")
async def create_upload(
    body: UploadCreateBody,
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_settings),
    storage: TelegramStorage = Depends(require_storage),
) -> dict[str, Any]:
    if body.parent_id:
        parent = db.get_item(body.parent_id)
        if not parent:
            raise HTTPException(404, "目标文件夹不存在")
        if parent["kind"] != "folder":
            raise HTTPException(400, "目标不是文件夹")
    if settings.max_file_size and body.size > settings.max_file_size:
        raise HTTPException(413, "文件超过配置的最大大小")
    temp_path = settings.temp_dir / f"upload_{secrets.token_hex(16)}.part"
    temp_path.touch(exist_ok=False)
    try:
        upload = db.create_upload(
            parent_id=body.parent_id,
            name=body.name,
            size=body.size,
            mime=body.mime,
            temp_path=str(temp_path),
            chunk_size=settings.upload_chunk_size,
        )
    except ValueError as exc:
        temp_path.unlink(missing_ok=True)
        raise HTTPException(400, str(exc)) from None
    return {
        "id": upload["id"],
        "offset": 0,
        "chunk_size": upload["chunk_size"],
        "size": upload["size"],
    }


@router.get("")
async def list_uploads(db: Database = Depends(get_db)) -> dict[str, Any]:
    """未完成的上传任务，用于刷新页面后断点续传。"""
    return {"uploads": [public_upload(item) for item in db.list_uploads()]}


@router.get("/{upload_id}")
async def get_upload(upload_id: str, db: Database = Depends(get_db)) -> dict[str, Any]:
    upload = db.get_upload(upload_id)
    if not upload:
        raise HTTPException(404, "上传任务不存在")
    return public_upload(upload)


@router.patch("/{upload_id}", response_model=None)
async def upload_chunk(
    upload_id: str,
    request: Request,
    db: Database = Depends(get_db),
) -> dict[str, Any] | JSONResponse:
    upload = db.get_upload(upload_id)
    if not upload:
        raise HTTPException(404, "上传任务不存在")
    if upload["state"] not in {"uploading", "error"}:
        raise HTTPException(409, f"当前状态不能继续上传: {upload['state']}")
    try:
        offset = int(request.headers.get("upload-offset", ""))
    except ValueError:
        raise HTTPException(400, "缺少或非法 Upload-Offset") from None
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
    async with aiofiles.open(path, "r+b") as fh:
        await fh.seek(offset)
        async for chunk in request.stream():
            if not chunk:
                continue
            if written + len(chunk) > chunk_limit:
                raise HTTPException(413, "分片超过约定大小")
            await fh.write(chunk)
            written += len(chunk)
    if written == 0:
        raise HTTPException(400, "空分片")

    new_offset = offset + written
    db.update_upload(upload_id, received=new_offset, state="uploading", error=None)
    return {"offset": new_offset, "received": new_offset, "size": upload["size"]}


@router.delete("/{upload_id}")
async def cancel_upload(upload_id: str, db: Database = Depends(get_db)) -> dict[str, bool]:
    upload = db.get_upload(upload_id)
    if not upload:
        raise HTTPException(404, "上传任务不存在")
    if upload["state"] == "processing":
        raise HTTPException(409, "正在写入 Telegram，无法取消")
    Path(upload["temp_path"]).unlink(missing_ok=True)
    db.delete_upload(upload_id)
    return {"ok": True}


@router.post("/{upload_id}/complete", response_model=None)
async def complete_upload(
    upload_id: str,
    db: Database = Depends(get_db),
    uploads: UploadService = Depends(get_upload_service),
    storage: TelegramStorage = Depends(require_storage),
) -> dict[str, Any] | JSONResponse:
    del storage  # dependency guarantees connectivity before scheduling work
    upload = db.get_upload(upload_id)
    if not upload:
        raise HTTPException(404, "上传任务不存在")
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
        {"ok": True, "upload": public_upload(db.get_upload(upload_id) or upload)}, status_code=202
    )
