from __future__ import annotations

import re

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from app.api.routes.files import content_disposition
from app.core.ranges import parse_range
from app.core.security import require_auth
from app.infrastructure.google_drive import (
    GoogleDrive,
    close_response,
    media_type,
    path_components,
    upstream_error,
)

router = APIRouter(
    prefix="/api/google-drive", tags=["google-drive"], dependencies=[Depends(require_auth)]
)
PRIVATE = {
    "Cache-Control": "private, no-store",
    "Cloudflare-CDN-Cache-Control": "no-store",
}
BYTE_RANGE = re.compile(r"bytes=(?:[0-9]{1,19}-[0-9]{0,19}|-[0-9]{1,19})")
CONTENT_RANGE = re.compile(r"bytes ([0-9]{1,19})-([0-9]{1,19})/([0-9]{1,19})")
UNSATISFIED_RANGE = re.compile(r"bytes \*/[0-9]{1,19}")


def get_google_drive(request: Request) -> GoogleDrive:
    if not request.app.state.settings.google_drive_enabled:
        raise HTTPException(404, "Google Drive 尚未配置")
    return request.app.state.google_drive


@router.get("/items")
async def items(
    path: str = Query("", max_length=4096), drive: GoogleDrive = Depends(get_google_drive)
) -> Response:
    entries = await drive.list_folder(path_components(path))
    return JSONResponse({"items": entries}, headers=PRIVATE)


class GoogleDriveResponse(StreamingResponse):
    def __init__(self, upstream: httpx.Response, mime: str, headers: dict[str, str]):
        self.upstream = upstream
        super().__init__(
            upstream.aiter_raw(), status_code=upstream.status_code, media_type=mime, headers=headers
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await close_response(self.upstream)


def verified_range(value: str, requested: str | None, length: str) -> int:
    match = CONTENT_RANGE.fullmatch(value)
    if requested and match:
        start, last, size = map(int, match.groups())
        expected_length = last - start + 1
        try:
            expected_start, expected_end, _ = parse_range(requested, size)
        except HTTPException:
            pass
        else:
            if (start, last + 1) == (expected_start, expected_end) and (
                not length or length == str(expected_length)
            ):
                return expected_length
    raise HTTPException(502, "OpenList 未返回有效的分段视频数据")


@router.get("/content")
async def content(
    request: Request,
    path: str = Query(..., min_length=1, max_length=4096),
    download: bool = False,
    drive: GoogleDrive = Depends(get_google_drive),
) -> Response:
    parts = path_components(path)
    requested_range = request.headers.get("range")
    if requested_range is not None and not BYTE_RANGE.fullmatch(requested_range):
        raise HTTPException(416, "只支持单段字节范围", headers=PRIVATE)
    forwarded = {"Range": requested_range} if requested_range else {}
    upstream = await drive.open("GET", drive.url(parts), forwarded)
    keep_open = False
    try:
        received_range = upstream.headers.get("content-range", "")
        headers = {
            **PRIVATE,
            "Content-Security-Policy": "sandbox",
            "X-Content-Type-Options": "nosniff",
        }
        if upstream.status_code == 416:
            if UNSATISFIED_RANGE.fullmatch(received_range):
                headers["Content-Range"] = received_range
            raise HTTPException(416, "Range 超出文件大小", headers=headers)
        if upstream.status_code not in {200, 206}:
            raise upstream_error(upstream.status_code)
        length = upstream.headers.get("content-length", "")
        if upstream.status_code == 206:
            length = str(verified_range(received_range, requested_range, length))
            headers["Content-Range"] = received_range
            headers["Accept-Ranges"] = "bytes"
        elif requested_range:
            raise HTTPException(502, "OpenList 未返回有效的分段视频数据")
        elif upstream.headers.get("accept-ranges") == "bytes":
            headers["Accept-Ranges"] = "bytes"
        if upstream.headers.get("content-encoding", "identity").lower() != "identity":
            raise HTTPException(502, "OpenList 返回了不支持的内容编码")
        if length.isascii() and length.isdecimal():
            headers["Content-Length"] = length
        mime = media_type(parts[-1])
        headers["Content-Disposition"] = content_disposition(
            parts[-1], inline=not download and mime != "application/octet-stream"
        )
        response = GoogleDriveResponse(upstream, mime, headers)
        keep_open = True
        return response
    finally:
        if not keep_open:
            await close_response(upstream)
