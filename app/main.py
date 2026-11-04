from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import mimetypes
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiofiles
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from app.auth import check_password, is_authenticated, require_auth
from app.config import ROOT_DIR, settings
from app.db import Database
from app.ranges import parse_range
from app.storage import TelegramStorage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("tgdrive")

db = Database(settings.db_path)
storage = TelegramStorage(settings)
background_tasks: set[asyncio.Task[Any]] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await storage.start()
    if storage.error:
        logger.warning("Telegram unavailable: %s", storage.error)
    yield
    for task in list(background_tasks):
        task.cancel()
    await storage.stop()
    db.close()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.app_secret,
    same_site="lax",
    https_only=settings.cookie_secure,
)
app.mount("/static", StaticFiles(directory=str(ROOT_DIR / "static")), name="static")


# ---------- request models ----------

class LoginBody(BaseModel):
    password: str


class FolderBody(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    parent_id: str | None = None


class UpdateItemBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    parent_id: str | None = None
    move: bool = False


class ShareBody(BaseModel):
    expires_hours: int = Field(default=0, ge=0, le=24 * 365)


class UploadCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0)
    mime: str | None = None
    parent_id: str | None = None


# ---------- helpers ----------

def ensure_folder(parent_id: str | None) -> dict[str, Any] | None:
    if not parent_id:
        return None
    parent = db.get_item(parent_id)
    if not parent:
        raise HTTPException(404, "目标文件夹不存在")
    if parent["kind"] != "folder":
        raise HTTPException(400, "目标不是文件夹")
    return parent


def require_storage() -> None:
    if not storage.status()["connected"]:
        raise HTTPException(503, storage.error or "Telegram 尚未连接")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(4 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def content_disposition(name: str, inline: bool) -> str:
    name = (name or "download").replace('"', "_").replace("\r", "").replace("\n", "")
    fallback = name.encode("ascii", "replace").decode("ascii")
    return f"{'inline' if inline else 'attachment'}; filename=\"{fallback}\"; filename*=UTF-8''{quote(name)}"


async def iter_item_range(item: dict[str, Any], start: int, end: int):
    parts = db.get_parts(item["id"])
    if not parts:
        raise FileNotFoundError("文件没有 Telegram 分段记录")
    cursor = 0
    for part in parts:
        part_start = cursor
        part_end = cursor + int(part["size"])
        cursor = part_end
        if part_end <= start or part_start >= end:
            continue
        local_start = max(start, part_start) - part_start
        local_end = min(end, part_end) - part_start
        async for chunk in storage.iter_download(int(part["tg_message_id"]), local_start, local_end - local_start):
            if chunk:
                yield chunk


async def stream_item(request: Request, item: dict[str, Any], public: bool = False):
    if item["kind"] != "file":
        raise HTTPException(400, "只能下载文件")
    if not storage.status()["connected"]:
        raise HTTPException(503, storage.error or "Telegram 尚未连接")
    size = int(item["size"])
    start, end, partial = parse_range(request.headers.get("range"), size)
    length = end - start
    mime = item.get("mime") or mimetypes.guess_type(item["name"])[0] or "application/octet-stream"
    inline = not request.query_params.get("download") and (
        mime.startswith("video/") or mime.startswith("audio/") or mime.startswith("image/") or mime == "application/pdf"
    )
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Content-Disposition": content_disposition(item["name"], inline=inline),
        "Cache-Control": "private, max-age=0, must-revalidate" if not public else "public, max-age=3600",
    }
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end - 1}/{size}"
    return StreamingResponse(
        iter_item_range(item, start, end),
        status_code=206 if partial else 200,
        media_type=mime,
        headers=headers,
    )


async def process_upload(upload_id: str) -> None:
    upload = db.get_upload(upload_id)
    if not upload:
        return
    path = Path(upload["temp_path"])
    if not path.exists():
        db.update_upload(upload_id, state="error", error="临时文件不存在，请重新上传")
        return

    db.update_upload(upload_id, state="processing", progress=0, error=None)
    last_update = 0.0

    async def progress(sent: int, total: int) -> None:
        nonlocal last_update
        now = time.monotonic()
        if now - last_update >= 0.8 or sent >= total:
            last_update = now
            db.update_upload(upload_id, progress=sent)

    try:
        sha256 = await asyncio.to_thread(file_sha256, path)
        parts = await storage.upload_file(
            path=path,
            name=upload["name"],
            mime=upload["mime"],
            progress=progress,
            storage_tag=upload_id,
        )
        item = db.create_file(
            name=upload["name"],
            parent_id=upload["parent_id"],
            size=int(upload["size"]),
            mime=upload["mime"],
            sha256=sha256,
            chat_id=str(storage.chat),
            parts=parts,
        )
        db.update_upload(upload_id, state="done", progress=int(upload["size"]), item_id=item["id"])
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove temp upload %s", path)
        logger.info("Upload completed: %s (%s bytes, %s parts)", item["name"], item["size"], len(parts))
    except Exception as exc:
        logger.exception("Upload failed: %s", upload_id)
        db.update_upload(upload_id, state="error", error=str(exc))


def schedule_upload(upload_id: str) -> None:
    task = asyncio.create_task(process_upload(upload_id))
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


# ---------- auth / meta ----------

@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(ROOT_DIR / "static" / "index.html")


@app.get("/api/meta")
async def meta(request: Request):
    return {
        "app_name": settings.app_name,
        "authenticated": is_authenticated(request),
        "storage": storage.status(),
        "chunk_size": settings.upload_chunk_size,
        "max_file_size": settings.max_file_size,
    }


@app.post("/api/login")
async def login(body: LoginBody, request: Request):
    if not check_password(body.password):
        raise HTTPException(401, "密码错误")
    request.session.clear()
    request.session["auth"] = True
    return {"ok": True}


@app.post("/api/logout")
async def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/stats", dependencies=[Depends(require_auth)])
async def stats():
    return db.stats()


# ---------- items ----------

@app.get("/api/items", dependencies=[Depends(require_auth)])
async def list_items(parent_id: str | None = None):
    ensure_folder(parent_id)
    items = db.list_items(parent_id)
    for item in items:
        item["download_url"] = f"/api/files/{item['id']}/download" if item["kind"] == "file" else None
    return {
        "items": items,
        "path": db.get_path(parent_id),
        "stats": db.stats(),
        "storage": storage.status(),
    }


@app.post("/api/folders", dependencies=[Depends(require_auth)])
async def create_folder(body: FolderBody):
    ensure_folder(body.parent_id)
    try:
        item = db.create_folder(body.name, body.parent_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return item


@app.patch("/api/items/{item_id}", dependencies=[Depends(require_auth)])
async def update_item(item_id: str, body: UpdateItemBody):
    item = db.get_item(item_id)
    if not item:
        raise HTTPException(404, "项目不存在")
    if body.move:
        ensure_folder(body.parent_id)
        if body.parent_id and (body.parent_id == item_id or db.is_descendant(body.parent_id, item_id)):
            raise HTTPException(400, "不能移动到自身或其子目录")
        db.move_item(item_id, body.parent_id)
    if body.name is not None:
        try:
            db.rename_item(item_id, body.name)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
    return db.get_item(item_id)


@app.delete("/api/items/{item_id}", dependencies=[Depends(require_auth)])
async def delete_item(item_id: str):
    item = db.get_item(item_id)
    if not item:
        raise HTTPException(404, "项目不存在")
    result = db.delete_tree(item_id)
    warning = None
    if result["message_ids"]:
        try:
            await storage.delete_messages(result["message_ids"])
        except Exception as exc:
            logger.exception("Failed to delete Telegram messages")
            warning = f"数据库已删除，但 Telegram 清理失败: {exc}"
    return {"ok": True, "deleted": result["deleted"], "warning": warning}


# ---------- resumable upload ----------

@app.post("/api/uploads", dependencies=[Depends(require_auth)])
async def create_upload(body: UploadCreateBody):
    require_storage()
    ensure_folder(body.parent_id)
    if settings.max_file_size and body.size > settings.max_file_size:
        raise HTTPException(413, "文件超过配置的最大大小")
    temp_path = settings.temp_dir / f"upload_{secrets.token_hex(16)}.part"
    with temp_path.open("wb") as f:
        f.truncate(0)
    try:
        upload = db.create_upload(
            parent_id=body.parent_id,
            name=body.name,
            size=body.size,
            mime=body.mime or mimetypes.guess_type(body.name)[0],
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


@app.get("/api/uploads/{upload_id}", dependencies=[Depends(require_auth)])
async def get_upload(upload_id: str):
    upload = db.get_upload(upload_id)
    if not upload:
        raise HTTPException(404, "上传任务不存在")
    return upload


@app.patch("/api/uploads/{upload_id}", dependencies=[Depends(require_auth)])
async def upload_chunk(upload_id: str, request: Request):
    upload = db.get_upload(upload_id)
    if not upload:
        raise HTTPException(404, "上传任务不存在")
    if upload["state"] not in {"uploading", "error"}:
        raise HTTPException(409, f"当前状态不能继续上传: {upload['state']}")
    try:
        offset = int(request.headers.get("upload-offset", ""))
    except ValueError:
        raise HTTPException(400, "缺少 Upload-Offset") from None
    received = int(upload["received"])
    if offset < received:
        # 客户端重发已写入的分片（上次响应丢失），幂等返回当前进度
        return {"offset": received, "received": received, "size": upload["size"], "duplicate": True}
    if offset > received:
        return JSONResponse({"error": "offset 不匹配", "offset": received}, status_code=409)

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) <= 0:
                raise HTTPException(400, "空分片")
        except ValueError:
            raise HTTPException(400, "Content-Length 无效") from None

    remaining = int(upload["size"]) - offset
    written = 0
    path = Path(upload["temp_path"])
    async with aiofiles.open(path, "r+b") as f:
        await f.seek(offset)
        async for chunk in request.stream():
            if not chunk:
                continue
            if written + len(chunk) > remaining:
                raise HTTPException(413, "分片超过文件剩余大小")
            await f.write(chunk)
            written += len(chunk)

    new_offset = offset + written
    db.update_upload(upload_id, received=new_offset, state="uploading", error=None)
    return {"offset": new_offset, "received": new_offset, "size": upload["size"]}


@app.get("/api/uploads", dependencies=[Depends(require_auth)])
async def list_uploads():
    """未完成的上传任务，用于刷新页面后断点续传。"""
    return {"uploads": db.list_uploads()}


@app.delete("/api/uploads/{upload_id}", dependencies=[Depends(require_auth)])
async def cancel_upload(upload_id: str):
    upload = db.get_upload(upload_id)
    if not upload:
        raise HTTPException(404, "上传任务不存在")
    if upload["state"] == "processing":
        raise HTTPException(409, "正在写入 Telegram，无法取消")
    Path(upload["temp_path"]).unlink(missing_ok=True)
    db.delete_upload(upload_id)
    return {"ok": True}


@app.post("/api/uploads/{upload_id}/complete", dependencies=[Depends(require_auth)])
async def complete_upload(upload_id: str):
    upload = db.get_upload(upload_id)
    if not upload:
        raise HTTPException(404, "上传任务不存在")
    if upload["state"] == "done":
        return upload
    if upload["state"] not in {"uploading", "error"}:
        return JSONResponse({"error": "正在处理中", "upload": upload}, status_code=202)
    if int(upload["received"]) != int(upload["size"]):
        raise HTTPException(400, f"文件尚未上传完成: {upload['received']}/{upload['size']}")
    require_storage()
    db.update_upload(upload_id, state="processing", progress=0, error=None)
    schedule_upload(upload_id)
    return JSONResponse({"ok": True, "upload": db.get_upload(upload_id)}, status_code=202)


# ---------- download / share ----------

@app.get("/api/files/{item_id}/download", dependencies=[Depends(require_auth)])
async def download_file(item_id: str, request: Request):
    item = db.get_item(item_id)
    if not item:
        raise HTTPException(404, "文件不存在")
    return await stream_item(request, item)


@app.post("/api/items/{item_id}/share", dependencies=[Depends(require_auth)])
async def create_share(item_id: str, body: ShareBody, request: Request):
    item = db.get_item(item_id)
    if not item:
        raise HTTPException(404, "文件不存在")
    if item["kind"] != "file":
        raise HTTPException(400, "目前只支持分享文件")
    token = secrets.token_urlsafe(24)
    expires_at = None
    if body.expires_hours:
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=body.expires_hours)).isoformat()
    db.create_share(item_id, token, expires_at)
    return {
        "token": token,
        "url": f"{str(request.base_url).rstrip('/')}/s/{token}",
        "expires_at": expires_at,
    }


def share_is_valid(share: dict[str, Any]) -> bool:
    expires_at = share.get("expires_at")
    if not expires_at:
        return True
    try:
        return datetime.fromisoformat(expires_at) > datetime.now(timezone.utc)
    except ValueError:
        return False


@app.get("/s/{token}", response_class=HTMLResponse)
async def share_page(token: str):
    share = db.get_share(token)
    if not share or not share_is_valid(share):
        raise HTTPException(404, "分享链接不存在或已过期")
    name = html.escape(share["name"])
    size = f"{int(share['size']) / 1024 / 1024:.1f} MB"
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{name} - {html.escape(settings.app_name)}</title>
<style>body{{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;display:grid;place-items:center;min-height:100vh;margin:0}}
.card{{background:#1e293b;padding:40px;border-radius:18px;max-width:520px;width:calc(100% - 48px);box-shadow:0 20px 60px #0006}}
h1{{font-size:20px;overflow-wrap:anywhere}}a{{display:inline-block;margin-top:20px;background:#38bdf8;color:#082f49;padding:12px 22px;border-radius:10px;text-decoration:none;font-weight:700}}</style>
</head><body><div class="card"><h1>{name}</h1><p>{size}</p><a href="/s/{token}/download">下载文件</a></div></body></html>"""


@app.get("/s/{token}/download")
async def share_download(token: str, request: Request):
    share = db.get_share(token)
    if not share or not share_is_valid(share):
        raise HTTPException(404, "分享链接不存在或已过期")
    item = db.get_item(share["item_id"])
    if not item:
        raise HTTPException(404, "文件不存在")
    return await stream_item(request, item, public=True)
