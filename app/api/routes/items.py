from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.deps import get_db, get_storage
from app.auth import require_auth
from app.db import Database
from app.schemas import FolderBody, ShareBody, UpdateItemBody
from app.storage import TelegramStorage

logger = logging.getLogger("tgdrive.items")
router = APIRouter(prefix="/api", tags=["items"], dependencies=[Depends(require_auth)])


def ensure_folder(db: Database, parent_id: str | None) -> dict[str, Any] | None:
    if not parent_id:
        return None
    parent = db.get_item(parent_id)
    if not parent:
        raise HTTPException(404, "目标文件夹不存在")
    if parent["kind"] != "folder":
        raise HTTPException(400, "目标不是文件夹")
    return parent


@router.get("/items")
async def list_items(
    parent_id: str | None = None, db: Database = Depends(get_db)
) -> dict[str, Any]:
    ensure_folder(db, parent_id)
    items = db.list_items(parent_id)
    for item in items:
        item["download_url"] = (
            f"/api/files/{item['id']}/download" if item["kind"] == "file" else None
        )
    return {
        "items": items,
        "path": db.get_path(parent_id),
        "stats": db.stats(),
    }


@router.post("/folders")
async def create_folder(body: FolderBody, db: Database = Depends(get_db)) -> dict[str, Any]:
    ensure_folder(db, body.parent_id)
    try:
        return db.create_folder(body.name, body.parent_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@router.patch("/items/{item_id}")
async def update_item(
    item_id: str, body: UpdateItemBody, db: Database = Depends(get_db)
) -> dict[str, Any]:
    item = db.get_item(item_id)
    if not item:
        raise HTTPException(404, "项目不存在")
    if body.move:
        ensure_folder(db, body.parent_id)
        if body.parent_id and (
            body.parent_id == item_id or db.is_descendant(body.parent_id, item_id)
        ):
            raise HTTPException(400, "不能移动到自身或其子目录")
        db.move_item(item_id, body.parent_id)
    if body.name is not None:
        try:
            db.rename_item(item_id, body.name)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
    return db.get_item(item_id)  # type: ignore[return-value]


@router.delete("/items/{item_id}")
async def delete_item(
    item_id: str,
    db: Database = Depends(get_db),
    storage: TelegramStorage = Depends(get_storage),
) -> dict[str, Any]:
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


@router.post("/items/{item_id}/share")
async def create_share(
    item_id: str,
    body: ShareBody,
    request: Request,
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    item = db.get_item(item_id)
    if not item:
        raise HTTPException(404, "文件不存在")
    if item["kind"] != "file":
        raise HTTPException(400, "目前只支持分享文件")
    token = secrets.token_urlsafe(24)
    expires_at = None
    if body.expires_hours:
        expires_at = (datetime.now(UTC) + timedelta(hours=body.expires_hours)).isoformat()
    db.create_share(item_id, token, expires_at)
    return {
        "token": token,
        "url": f"{str(request.base_url).rstrip('/')}/s/{token}",
        "expires_at": expires_at,
    }
