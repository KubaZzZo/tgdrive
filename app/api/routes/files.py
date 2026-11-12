from __future__ import annotations

import hashlib
import html
import logging
import mimetypes
from collections.abc import AsyncGenerator
from contextlib import aclosing
from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from urllib.parse import quote

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from starlette.types import Send
from telethon.errors import RPCError

from app.api.deps import get_db, get_settings, get_storage
from app.auth import require_auth
from app.config import Settings
from app.db import Database
from app.ranges import parse_range
from app.services.cdn import CDNMonitor
from app.services.image_host import MAX_IMAGE_SIZE
from app.services.telegram_bot import MAX_BOT_DOWNLOAD, BotAPIError
from app.services.thumbnails import VIDEO_SAMPLE_BYTES, Thumbnail, image_thumbnail, video_thumbnail
from app.storage import PREFETCH_MIN_BYTES, TelegramStorage

router = APIRouter(tags=["files"])
auth_router = APIRouter(prefix="/api", tags=["files"], dependencies=[Depends(require_auth)])
logger = logging.getLogger("tgdrive.media")


class FileStreamingResponse(StreamingResponse):
    body_iterator: AsyncGenerator[bytes, None]

    async def stream_response(self, send: Send) -> None:
        try:
            await super().stream_response(send)
        finally:
            # A disconnect can interrupt send() while the iterator is suspended.
            with anyio.CancelScope(shield=True):
                await self.body_iterator.aclose()


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
    *,
    prefetch: bool = False,
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
        length = local_end - local_start
        options = {"prefetch": True} if prefetch and length >= PREFETCH_MIN_BYTES else {}
        async with aclosing(
            storage.iter_download(int(part["tg_message_id"]), local_start, length, **options)
        ) as stream:
            async for chunk in stream:
                if chunk:
                    if cdn:
                        await cdn.charge(len(chunk))
                    yield chunk


async def trace_media_range(stream, request_id: str, expected: int, mode: str):
    started = perf_counter()
    first_ms = None
    sent = 0
    try:
        async with aclosing(stream):
            async for chunk in stream:
                if first_ms is None:
                    first_ms = round((perf_counter() - started) * 1000, 1)
                sent += len(chunk)
                yield chunk
    finally:
        logger.info(
            "Media %s origin_first_ms=%s origin_total_ms=%.1f bytes_yielded=%d expected=%d",
            mode,
            first_ms,
            (perf_counter() - started) * 1000,
            sent,
            expected,
            extra={"request_id": request_id},
        )


async def charge_media_stream(stream, cdn: CDNMonitor):
    async with aclosing(stream):
        async for chunk in stream:
            await cdn.charge(len(chunk))
            yield chunk


async def stream_item(
    request: Request,
    item: dict[str, Any],
    db: Database,
    storage: TelegramStorage,
    public: bool = False,
    check_only: bool = False,
) -> Response:
    if item["kind"] != "file":
        raise HTTPException(400, "只能下载文件")
    size = int(item["size"])
    bot = None
    bot_file_id = None
    if not storage.status()["connected"]:
        bot = getattr(request.state.telegram_runtime.sync, "bot", None)
        if not bot:
            raise HTTPException(503, storage.error or "Telegram 尚未连接")
        bot_file_id = db.bot_file_id(item["id"], bot.id)
        if not bot_file_id:
            raise HTTPException(503, "此文件需要个人 Telegram 会话下载。")
        if size > MAX_BOT_DOWNLOAD:
            raise HTTPException(503, "此文件超过 Bot API 的 20 MB 下载上限，需要个人会话。")
    cdn = request.app.state.cdn
    parts = db.get_parts(item["id"])
    if (
        not bot
        and size
        and (
            not parts
            or any(int(p["size"]) <= 0 for p in parts)
            or sum(int(p["size"]) for p in parts) != size
        )
    ):
        raise HTTPException(503, "文件分段记录不完整，请重新同步频道后重试")
    # Include the message mapping: channel edits can replace content at the same URL.
    version = repr(
        (
            item["id"],
            item.get("sha256"),
            bot_file_id,
            item.get("updated_at"),
            size,
            [(p["tg_message_id"], p["size"]) for p in parts],
        )
    )
    etag = '"' + hashlib.sha256(version.encode()).hexdigest() + '"'
    range_header = None if check_only else request.headers.get("range")
    if request.headers.get("if-range") not in (None, etag):
        range_header = None
    start, end, partial = parse_range(range_header, size)
    length = end - start
    await cdn.check(length)
    if check_only:
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
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
    if request.method == "HEAD" or not length:
        return Response(status_code=206 if partial else 200, media_type=mime, headers=headers)
    probe = request.headers.get("X-TGDrive-Media-Probe") == "1"
    trace = (
        not public
        and inline
        and mime.startswith("video/")
        and (start == 0 or (probe and partial and length <= 1024 * 1024))
    )
    mode = "probe" if probe else "initial-range"
    if bot and bot_file_id:
        started = perf_counter()
        try:
            data, received = await bot.read(bot_file_id, size, start, end)
        except BotAPIError as exc:
            raise HTTPException(502, str(exc)) from None
        request.app.state.traffic.record("telegram", inbound=received)
        if trace:
            logger.info(
                "Media %s bot_read_ms=%.1f bytes_received=%d",
                mode,
                (perf_counter() - started) * 1000,
                received,
            )
        await cdn.charge(len(data))
        return Response(data, status_code=206 if partial else 200, media_type=mime, headers=headers)
    cache = request.app.state.media_cache
    if cache.enabled and not public and partial and inline and item.get("category") == "video":

        def fetch_slice(first: int, last: int):
            return iter_item_range(db, storage, item, first, last, parts)

        stream = charge_media_stream(
            cache.stream(db.path, etag, start, end, size, fetch_slice), cdn
        )
    else:
        stream = iter_item_range(db, storage, item, start, end, parts, cdn, prefetch=not inline)
    if trace:
        stream = trace_media_range(stream, request.state.request_id, length, mode)
    return FileStreamingResponse(
        stream,
        status_code=206 if partial else 200,
        media_type=mime,
        headers=headers,
    )


@auth_router.api_route("/files/{item_id}/download", methods=["GET", "HEAD"])
@auth_router.get("/files/{item_id}/download/check")
async def download_file(
    item_id: str,
    request: Request,
    db: Database = Depends(get_db),
    storage: TelegramStorage = Depends(get_storage),
) -> Response:
    item = db.get_item(item_id)
    if not item:
        raise HTTPException(404, "文件不存在")
    return await stream_item(
        request, item, db, storage, check_only=request.url.path.endswith("/check")
    )


@auth_router.get("/files/{item_id}/thumbnail")
async def file_thumbnail(
    item_id: str,
    request: Request,
    db: Database = Depends(get_db),
    storage: TelegramStorage = Depends(get_storage),
) -> Response:
    item = db.get_item(item_id)
    if not item or item.get("category") not in {"video", "image"}:
        raise HTTPException(404, "封面不存在", headers={"Cache-Control": "no-store"})
    cdn = request.app.state.cdn
    await cdn.check()
    key = (db.path, item_id, item["updated_at"])
    try:
        result = await request.app.state.thumbnails.get(
            key, lambda: generate_thumbnail(item, request, db, storage)
        )
    except (TimeoutError, OSError, RPCError, BotAPIError):
        result = None
    if not result:
        raise HTTPException(404, "暂无封面", headers={"Cache-Control": "no-store"})
    data, mime = result
    etag = '"' + hashlib.sha256(data).hexdigest() + '"'
    headers = {
        "Cache-Control": "private, no-cache",
        "Cloudflare-CDN-Cache-Control": "no-store",
        "ETag": etag,
        "X-Content-Type-Options": "nosniff",
    }
    if any(
        tag.strip().removeprefix("W/") in {"*", etag}
        for tag in request.headers.get("if-none-match", "").split(",")
    ):
        return Response(status_code=304, headers=headers)
    await cdn.charge(len(data))
    return Response(data, media_type=mime, headers=headers)


async def generate_thumbnail(
    item: dict[str, Any],
    request: Request,
    db: Database,
    storage: TelegramStorage,
) -> Thumbnail | None:
    bot = getattr(request.state.telegram_runtime.sync, "bot", None)
    item_id = item["id"]
    video = item["category"] == "video"
    connected = storage.status()["connected"]
    parts = db.get_parts(item_id) if connected else []
    if video:
        cover_id = db.bot_file_id(item_id, bot.id, cover=True) if bot else None
        if bot and cover_id:
            with anyio.move_on_after(3):
                try:
                    cover = await bot.thumbnail(cover_id)
                except BotAPIError:
                    pass
                else:
                    request.app.state.traffic.record("telegram", inbound=len(cover))
                    if cover.startswith(b"\xff\xd8\xff"):
                        return cover, "image/jpeg"
                    if cover.startswith(b"\x89PNG\r\n\x1a\n"):
                        return cover, "image/png"
                    if cover.startswith(b"RIFF") and cover[8:12] == b"WEBP":
                        return cover, "image/webp"
        if parts:
            with anyio.move_on_after(3):
                try:
                    cover = await storage.thumbnail(int(parts[0]["tg_message_id"]))
                except (OSError, RPCError):
                    pass
                else:
                    if cover:
                        return cover, "image/jpeg"
    size = int(item["size"])
    limit = VIDEO_SAMPLE_BYTES if video else MAX_IMAGE_SIZE
    if size <= 0 or (not video and size > limit):
        return None
    if parts:
        end = min(size, limit)
        sample = bytearray()
        async with aclosing(iter_item_range(db, storage, item, 0, end, parts)) as stream:
            async for chunk in stream:
                if len(sample) + len(chunk) > end:
                    return None
                sample.extend(chunk)
        if len(sample) != end:
            return None
        data = bytes(sample)
    elif bot and size <= limit and (file_id := db.bot_file_id(item_id, bot.id)):
        data, received = await bot.read(file_id, size, 0, size)
        request.app.state.traffic.record("telegram", inbound=received)
    else:
        return None
    if video:
        return await video_thumbnail(data)
    return await anyio.to_thread.run_sync(image_thumbnail, data)


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
