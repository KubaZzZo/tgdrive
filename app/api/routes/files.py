from __future__ import annotations

import hashlib
import html
import mimetypes
from contextlib import aclosing
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse

from app.api.deps import get_db, get_settings, get_storage
from app.auth import require_auth
from app.config import Settings
from app.db import Database
from app.ranges import parse_range
from app.services.cdn import CDNMonitor
from app.storage import TelegramStorage

router = APIRouter(tags=["files"])
auth_router = APIRouter(prefix="/api", tags=["files"], dependencies=[Depends(require_auth)])


def content_disposition(name: str, inline: bool) -> str:
    name = (name or "download").replace('"', "_").replace("\r", "").replace("\n", "")
    fallback = name.encode("ascii", "replace").decode("ascii")
    return f"{'inline' if inline else 'attachment'}; filename=\"{fallback}\"; filename*=UTF-8''{quote(name)}"


async def iter_item_range(
    db: Database,
    storage: TelegramStorage,
    item: dict[str, Any],
    start: int,
    end: int,
    parts: list[dict[str, Any]] | None = None,
    cdn: CDNMonitor | None = None,
):
    parts = db.get_parts(item["id"]) if parts is None else parts
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
        async with aclosing(
            storage.iter_download(int(part["tg_message_id"]), local_start, local_end - local_start)
        ) as stream:
            async for chunk in stream:
                if chunk:
                    if cdn:
                        await cdn.charge(len(chunk))
                    yield chunk


async def stream_item(
    request: Request,
    item: dict[str, Any],
    db: Database,
    storage: TelegramStorage,
    public: bool = False,
) -> Response:
    if item["kind"] != "file":
        raise HTTPException(400, "只能下载文件")
    cdn = request.app.state.cdn
    if not storage.status()["connected"]:
        raise HTTPException(503, storage.error or "Telegram 尚未连接")
    size = int(item["size"])
    parts = db.get_parts(item["id"])
    if size and (
        not parts
        or any(int(p["size"]) <= 0 for p in parts)
        or sum(int(p["size"]) for p in parts) != size
    ):
        raise HTTPException(503, "文件分段记录不完整，请重新同步频道后重试")
    # Include the message mapping: channel edits can replace content at the same URL.
    version = repr(
        (
            item["id"],
            item.get("sha256"),
            item.get("updated_at"),
            size,
            [(p["tg_message_id"], p["size"]) for p in parts],
        )
    )
    etag = '"' + hashlib.sha256(version.encode()).hexdigest() + '"'
    range_header = request.headers.get("range")
    if request.headers.get("if-range") not in (None, etag):
        range_header = None
    start, end, partial = parse_range(range_header, size)
    length = end - start
    await cdn.check(length)
    mime = item.get("mime") or mimetypes.guess_type(item["name"])[0] or "application/octet-stream"
    inline = not request.query_params.get("download") and (
        mime.startswith("video/")
        or mime.startswith("audio/")
        or mime.startswith("image/")
        or mime == "application/pdf"
    )
    headers = {
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "X-Content-Type-Options": "nosniff",
        "Content-Length": str(length),
        "Content-Disposition": content_disposition(item["name"], inline=inline),
        "Cache-Control": (
            "public, max-age=3600" if public else "private, max-age=0, must-revalidate"
        ),
    }
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end - 1}/{size}"
    if request.method == "HEAD":
        return Response(status_code=206 if partial else 200, media_type=mime, headers=headers)
    return StreamingResponse(
        iter_item_range(db, storage, item, start, end, parts, cdn) if length else iter(()),
        status_code=206 if partial else 200,
        media_type=mime,
        headers=headers,
    )


@auth_router.api_route("/files/{item_id}/download", methods=["GET", "HEAD"])
async def download_file(
    item_id: str,
    request: Request,
    db: Database = Depends(get_db),
    storage: TelegramStorage = Depends(get_storage),
) -> Response:
    item = db.get_item(item_id)
    if not item:
        raise HTTPException(404, "文件不存在")
    return await stream_item(request, item, db, storage)


@auth_router.get("/files/{item_id}/thumbnail")
async def file_thumbnail(
    item_id: str,
    request: Request,
    db: Database = Depends(get_db),
    storage: TelegramStorage = Depends(get_storage),
) -> Response:
    item = db.get_item(item_id)
    if not item or item.get("category") != "video":
        raise HTTPException(404, "视频不存在")
    parts = db.get_parts(item_id)
    if not parts:
        raise HTTPException(404, "没有封面")
    await request.app.state.cdn.check()
    data = await storage.thumbnail(int(parts[0]["tg_message_id"]))
    if not data:
        raise HTTPException(404, "Telegram 未提供封面")
    await request.app.state.cdn.charge(len(data))
    return Response(
        data,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"},
    )


def share_is_valid(share: dict[str, Any]) -> bool:
    expires_at = share.get("expires_at")
    if not expires_at:
        return True
    try:
        return datetime.fromisoformat(expires_at) > datetime.now(UTC)
    except ValueError:
        return False


@router.get("/s/{token}", response_class=HTMLResponse)
async def share_page(
    token: str,
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> str:
    share = db.get_share(token)
    if not share or not share_is_valid(share):
        raise HTTPException(404, "分享链接不存在或已过期")
    name = html.escape(share["name"])
    size = f"{int(share['size']) / 1024 / 1024:.1f} MB"
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{name} - {html.escape(settings.app_name)}</title>
<style>
:root{{color-scheme:dark}}*{{box-sizing:border-box}}body{{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;background:radial-gradient(800px 500px at 20% 0%,#0c4a6e55,transparent),#08111f;color:#e6eefb;display:grid;place-items:center;min-height:100vh;margin:0;padding:24px}}
.card{{background:#101d31;padding:40px;border:1px solid #263852;border-radius:20px;max-width:520px;width:100%;box-shadow:0 24px 80px #0008}}
.badge{{color:#38bdf8;font-size:12px;font-weight:800;letter-spacing:.12em}}h1{{font-size:22px;overflow-wrap:anywhere;margin:12px 0 8px}}p{{color:#8ea3c1}}a{{display:inline-block;margin-top:18px;background:linear-gradient(135deg,#38bdf8,#60a5fa);color:#082f49;padding:12px 22px;border-radius:10px;text-decoration:none;font-weight:800}}
</style></head><body><div class="card"><div class="badge">TG DRIVE SHARE</div><h1>{name}</h1><p>{size}</p><a href="/s/{token}/download?download=1">下载文件</a></div></body></html>"""


@router.api_route("/s/{token}/download", methods=["GET", "HEAD"])
async def share_download(
    token: str,
    request: Request,
    db: Database = Depends(get_db),
    storage: TelegramStorage = Depends(get_storage),
) -> Response:
    share = db.get_share(token)
    if not share or not share_is_valid(share):
        raise HTTPException(404, "分享链接不存在或已过期")
    item = db.get_item(share["item_id"])
    if not item:
        raise HTTPException(404, "文件不存在")
    return await stream_item(request, item, db, storage, public=True)
