from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from app.api.deps import get_db, get_storage, require_storage
from app.api.routes.files import iter_item_range, stream_item
from app.core.security import require_auth
from app.infrastructure.db import Database
from app.infrastructure.storage import TelegramStorage
from app.services.image_host import IMAGE_MIMES, MAX_IMAGE_SIZE, image_mime

router = APIRouter(prefix="/api/images", tags=["images"], dependencies=[Depends(require_auth)])
public_router = APIRouter(tags=["images"])


def image_url(token: str) -> str:
    return f"/i/{token}"


@router.get("")
async def list_images(
    q: str = Query(default="", max_length=255),
    limit: int = Query(default=24, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Database = Depends(get_db),
) -> dict:
    rows, total = db.list_hosted_images(q.strip(), limit, offset)
    return {"images": [{**r, "url": image_url(r["token"])} for r in rows], "total": total}


@router.post("/{item_id}")
async def publish_image(
    item_id: str,
    request: Request,
    db: Database = Depends(get_db),
    storage: TelegramStorage = Depends(require_storage),
) -> dict:
    item = db.get_item(item_id)
    if not item or item["kind"] != "file":
        raise HTTPException(404, "图片不存在")
    if not 0 < int(item["size"]) <= MAX_IMAGE_SIZE:
        raise HTTPException(413, "图床单张图片不能超过 20 MiB")
    await request.app.state.cdn.check(32)
    header = bytearray()
    async for chunk in iter_item_range(
        db, storage, item, 0, min(32, item["size"]), cdn=request.app.state.cdn
    ):
        header.extend(chunk)
    try:
        mime = image_mime(bytes(header))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    try:
        hosted = db.publish_image(item_id, secrets.token_urlsafe(24), mime)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    return {**hosted, "url": image_url(hosted["token"])}


@router.delete("/{item_id}")
async def unpublish_image(item_id: str, db: Database = Depends(get_db)) -> dict:
    db.unpublish_image(item_id)
    return {"ok": True}


@public_router.api_route("/i/{token}", methods=["GET", "HEAD"])
async def public_image(
    token: str,
    request: Request,
    db: Database = Depends(get_db),
    storage: TelegramStorage = Depends(get_storage),
) -> Response:
    hosted = db.hosted_image(token)
    item = db.get_item(hosted["item_id"]) if hosted else None
    if not item or hosted["mime"] not in IMAGE_MIMES:
        raise HTTPException(404, "图片链接不存在或已撤销")
    # The actual image type was verified when published. Never trust the filename.
    response = await stream_item(request, {**item, "mime": hosted["mime"]}, db, storage)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Cloudflare-CDN-Cache-Control"] = "no-store"
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return response
