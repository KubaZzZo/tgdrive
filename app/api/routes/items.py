from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.deps import get_db
from app.api.schemas import (
    BatchItemsBody,
    FolderBody,
    FolderPathsBody,
    PeopleLabelBody,
    ShareBody,
    ShareTokenBody,
    UpdateItemBody,
    UpdateShareBody,
    VideoPeopleBody,
)
from app.core.security import require_auth
from app.infrastructure.db import Database, utcnow
from app.services.categories import Category
from app.services.people import MAX_PERSON_LENGTH, person_key
from app.services.telegram_cleanup import TRASH_RETENTION_DAYS

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
    parent_id: str | None = None,
    scope: Literal["folder", "all", "recent"] = "folder",
    category: Category | None = None,
    q: str = Query(default="", max_length=255),
    person: str | None = Query(default=None, min_length=1, max_length=MAX_PERSON_LENGTH),
    kind: Literal["file", "folder"] | None = None,
    created_from: date | None = None,
    created_to: date | None = None,
    min_size: int | None = Query(default=None, ge=0, le=2**63 - 1),
    max_size: int | None = Query(default=None, ge=0, le=2**63 - 1),
    extension: str | None = Query(default=None, pattern=r"^\.?[a-zA-Z0-9][a-zA-Z0-9._+-]{0,31}$"),
    sort: Literal["name", "size", "created_at", "updated_at"] = "name",
    direction: Literal["asc", "desc"] = "asc",
    limit: int = Query(default=60, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=2**63 - 1),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    if created_from and created_to and created_from > created_to:
        raise HTTPException(400, "起始日期不能晚于结束日期")
    if min_size is not None and max_size is not None and min_size > max_size:
        raise HTTPException(400, "最小文件大小不能超过最大文件大小")
    ensure_folder(db, parent_id)
    items, total = db.query_items(
        parent_id,
        scope=scope,
        category=category,
        query=q.strip(),
        person=person,
        kind=kind,
        created_from=created_from.isoformat() if created_from else None,
        created_to=created_to.isoformat() if created_to else None,
        min_size=min_size,
        max_size=max_size,
        extension=extension.removeprefix(".") if extension else None,
        sort=sort,
        direction=direction,
        limit=limit,
        offset=offset,
    )
    for item in items:
        item["download_url"] = (
            f"/api/files/{item['id']}/download" if item["kind"] == "file" else None
        )
    return {
        "items": items,
        "path": db.get_path(parent_id),
        "categories": db.category_stats(),
        "total": total,
        "limit": limit,
        "offset": offset,
        "stats": db.stats(),
        **({"person_display_name": db.video_person_display_name(person)} if person else {}),
    }


@router.get("/people")
async def list_video_people(
    q: str = Query(default="", max_length=MAX_PERSON_LENGTH),
    limit: int = Query(default=48, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    people, total = db.list_video_people(q, limit, offset)
    return {
        "people": [{**label, "key": person_key(label["name"])} for label in people],
        "total": total,
    }


@router.post("/people")
async def create_people_label(
    body: PeopleLabelBody, db: Database = Depends(get_db)
) -> dict[str, bool]:
    try:
        db.create_people_label(body.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"ok": True}


@router.delete("/people")
async def delete_people_label(
    name: str = Query(min_length=1, max_length=MAX_PERSON_LENGTH),
    db: Database = Depends(get_db),
) -> dict[str, bool]:
    try:
        db.delete_people_label(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"ok": True}


@router.get("/items/{item_id}/people")
async def get_video_people(item_id: str, db: Database = Depends(get_db)) -> dict[str, Any]:
    try:
        return {"people": db.video_people_labels(item_id)}
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@router.patch("/items/{item_id}/people")
async def edit_video_people(
    item_id: str, body: VideoPeopleBody, db: Database = Depends(get_db)
) -> dict[str, bool]:
    try:
        db.edit_video_people(item_id, body.add, body.remove)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"ok": True}


@router.post("/folders")
async def create_folder(body: FolderBody, db: Database = Depends(get_db)) -> dict[str, Any]:
    try:
        return db.create_folder(body.name, body.parent_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@router.patch("/items/{item_id}")
async def update_item(
    item_id: str, body: UpdateItemBody, db: Database = Depends(get_db)
) -> dict[str, Any]:
    try:
        item = db.update_item(item_id, name=body.name, move=body.move, parent_id=body.parent_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    if not item:
        raise HTTPException(404, "项目不存在")
    return item


@router.post("/folders/paths")
async def create_folder_paths(body: FolderPathsBody, db: Database = Depends(get_db)) -> dict:
    try:
        return {"ids": db.create_folder_paths(body.parent_id, body.paths)}
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@router.post("/items/batch")
async def batch_items(body: BatchItemsBody, db: Database = Depends(get_db)) -> dict:
    ids = list(dict.fromkeys(body.ids))
    if body.action == "trash":
        trashed = db.trash_items(ids)
        return {
            "results": [
                {"id": item_id, "ok": True}
                if item_id in trashed
                else {"id": item_id, "ok": False, "error": "项目不存在"}
                for item_id in ids
            ]
        }
    results = []
    for item_id in ids:
        try:
            if not db.move_item(item_id, body.parent_id):
                raise LookupError("项目不存在")
            results.append({"id": item_id, "ok": True})
        except (LookupError, ValueError) as exc:
            results.append({"id": item_id, "ok": False, "error": str(exc)})
    return {"results": results}


@router.delete("/items/{item_id}")
async def delete_item(
    item_id: str,
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    deleted = db.trash_tree(item_id)
    if not deleted:
        raise HTTPException(404, "项目不存在")
    return {"ok": True, "deleted": deleted, "warning": None}


@router.get("/trash")
async def list_trash(
    limit: int = Query(default=30, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=2**63 - 1),
    db: Database = Depends(get_db),
) -> dict:
    rows, total = db.list_trash(limit, offset)
    return {"items": rows, "total": total, "retention_days": TRASH_RETENTION_DAYS}


@router.post("/trash/{item_id}/restore")
async def restore_trash(item_id: str, db: Database = Depends(get_db)) -> dict:
    item = db.restore_trash(item_id)
    if not item:
        raise HTTPException(404, "回收站中没有此项目")
    return item


@router.delete("/trash/{item_id}")
async def purge_trash_item(
    item_id: str,
    request: Request,
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    runtime = request.state.telegram_runtime
    chat = runtime.storage.chat
    result = db.delete_tree(
        item_id, fallback_chat_id=str(chat) if chat is not None else "", trashed_before=utcnow()
    )
    if not result["deleted"]:
        raise HTTPException(404, "回收站中没有此项目")
    warning = None
    if result["message_ids"]:
        try:
            await asyncio.wait_for(runtime.cleanup.process(result["batch_id"]), timeout=5)
        except TimeoutError:
            pass
        if db.has_telegram_cleanup(result["batch_id"]):
            warning = "条目已删除，Telegram 消息仍在等待清理"
    return {"ok": True, "deleted": result["deleted"], "warning": warning}


@router.delete("/trash")
async def empty_trash(request: Request) -> dict:
    cleanup = request.state.telegram_runtime.cleanup
    deleted = await asyncio.to_thread(cleanup.purge_trash, utcnow())
    return {"ok": True, "deleted": deleted}


def share_expiry(hours: int) -> str | None:
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat() if hours else None


def share_url(request: Request, token: str) -> str:
    return f"{str(request.base_url).rstrip('/')}/s/{token}"


@router.get("/shares")
async def list_shares(
    request: Request,
    limit: int = Query(default=30, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=2**63 - 1),
    db: Database = Depends(get_db),
) -> dict:
    rows, total = db.list_shares(limit, offset)
    return {
        "shares": [{**row, "url": share_url(request, row["token"])} for row in rows],
        "total": total,
    }


@router.patch("/shares")
async def update_share(body: UpdateShareBody, db: Database = Depends(get_db)) -> dict:
    expires_at = share_expiry(body.expires_hours)
    if not db.update_share(body.token, expires_at):
        raise HTTPException(404, "分享链接不存在")
    return {"expires_at": expires_at}


@router.delete("/shares")
async def revoke_share(body: ShareTokenBody, db: Database = Depends(get_db)) -> dict:
    if not db.delete_share(body.token):
        raise HTTPException(404, "分享链接不存在")
    return {"ok": True}


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
    expires_at = share_expiry(body.expires_hours)
    try:
        db.create_share(item_id, token, expires_at)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    return {
        "token": token,
        "url": share_url(request, token),
        "expires_at": expires_at,
    }
