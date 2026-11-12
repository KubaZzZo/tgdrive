from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.deps import get_db
from app.auth import require_auth
from app.categories import Category
from app.db import Database
from app.people import MAX_PERSON_LENGTH, person_key
from app.schemas import FolderBody, PeopleLabelBody, ShareBody, UpdateItemBody, VideoPeopleBody

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
    sort: Literal["name", "size", "created_at", "updated_at"] = "name",
    direction: Literal["asc", "desc"] = "asc",
    limit: int = Query(default=60, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    if scope == "folder":
        ensure_folder(db, parent_id)
    items, total = db.query_items(
        parent_id,
        scope=scope,
        category=category,
        query=q.strip(),
        person=person,
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
        "path": db.get_path(parent_id) if scope == "folder" else [],
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
    ensure_folder(db, body.parent_id)
    try:
        return db.create_folder(body.name, body.parent_id)
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


@router.delete("/items/{item_id}")
async def delete_item(
    item_id: str,
    request: Request,
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    runtime = request.state.telegram_runtime
    chat = runtime.storage.chat
    result = db.delete_tree(item_id, fallback_chat_id=str(chat) if chat is not None else "")
    if not result["deleted"]:
        raise HTTPException(404, "项目不存在")
    warning = None
    if result["message_ids"]:
        await runtime.cleanup.process(result["batch_id"])
        if db.has_telegram_cleanup(result["batch_id"]):
            warning = "条目已删除，Telegram 消息仍在等待清理"
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
