from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime
from email.utils import format_datetime
from urllib.parse import quote
from xml.etree import ElementTree as XML

from defusedxml import ElementTree as SafeXML
from defusedxml.common import DefusedXmlException
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from app.api.client_auth import require_channel, require_webdav
from app.api.deps import get_db, get_storage
from app.api.request_body import bounded_body
from app.api.routes.files import content_disposition, stream_item
from app.infrastructure.db import Database
from app.infrastructure.storage import TelegramStorage

router = APIRouter(prefix="/webdav", tags=["webdav"], dependencies=[Depends(require_webdav)])
DAV = "{DAV:}"
METHODS = ["OPTIONS", "PROPFIND", "GET", "HEAD"]
HEADERS = {"DAV": "1", "Allow": ", ".join(METHODS), "Cache-Control": "private, no-store"}
SUFFIX = re.compile(r"~[0-9a-f]{32}$")
INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
XML.register_namespace("D", "DAV:")


def members(db: Database, parent_id: str | None, chat_id: str) -> dict[str, dict]:
    items = [
        item
        for item in db.list_items(parent_id)
        if item["kind"] == "folder" or item["tg_chat_id"] == chat_id
    ]
    counts = Counter(item["name"] for item in items)
    return {
        (f"{name}~{item['id']}" if counts[name] > 1 or SUFFIX.search(name) else name): item
        for item in items
        for name in (item["name"],)
    }


def resolve(db: Database, path: list[str], chat_id: str) -> dict:
    item: dict = {"id": None, "kind": "folder", "name": "TG Drive"}
    for component in path:
        if item["kind"] != "folder" or component in {"", ".", ".."}:
            raise HTTPException(404, "资源不存在")
        children = members(db, item["id"], chat_id)
        found = children.get(component)
        if found is None and SUFFIX.search(component):
            name, _, item_id = component.rpartition("~")
            candidate = children.get(name)
            if candidate and candidate["id"] == item_id:
                found = candidate
        if found is None:
            raise HTTPException(404, "资源不存在")
        item = found
    return item


async def requested_properties(request: Request) -> tuple[str, list[str]]:
    body = b"".join([chunk async for chunk in bounded_body(request, 16 * 1024)])
    if not body:
        return "allprop", []
    try:
        root = SafeXML.fromstring(body, forbid_dtd=True)
    except (XML.ParseError, DefusedXmlException, ValueError):
        raise HTTPException(400, "无效的 WebDAV 属性请求") from None
    if root.tag != DAV + "propfind":
        raise HTTPException(400, "无效的 WebDAV 属性请求")
    for child in root:
        if child.tag in {DAV + "prop", DAV + "propname", DAV + "allprop"}:
            names = list(dict.fromkeys(prop.tag for prop in child))
            if len(names) > 64:
                raise HTTPException(413, "WebDAV 属性过多")
            return child.tag.removeprefix(DAV), names
    raise HTTPException(400, "缺少 WebDAV 属性选择")


def add_properties(
    root: XML.Element, href: str, item: dict, mode: str, requested: list[str]
) -> None:
    response = XML.SubElement(root, DAV + "response")
    folder = item["kind"] == "folder"
    XML.SubElement(response, DAV + "href").text = href
    values: dict[str, str | None] = {
        DAV + "displayname": item["name"],
        DAV + "resourcetype": None,
    }
    if item.get("created_at"):
        values[DAV + "creationdate"] = item["created_at"]
    if item.get("updated_at"):
        values[DAV + "getlastmodified"] = format_datetime(
            datetime.fromisoformat(item["updated_at"]).astimezone(UTC), usegmt=True
        )
    if not folder:
        values[DAV + "getcontentlength"] = str(item["size"])
        values[DAV + "getcontenttype"] = item["mime"] or "application/octet-stream"

    names = requested if mode == "prop" else list(values)
    known = [name for name in names if name in values]
    missing = [name for name in names if name not in values]
    for names, status in (
        (known, "HTTP/1.1 200 OK"),
        (missing, "HTTP/1.1 404 Not Found"),
    ):
        if not names:
            continue
        stat = XML.SubElement(response, DAV + "propstat")
        props = XML.SubElement(stat, DAV + "prop")
        for name in names:
            element = XML.SubElement(props, name)
            if mode == "propname" or name not in values:
                continue
            if name == DAV + "resourcetype" and folder:
                XML.SubElement(element, DAV + "collection")
            elif (value := values[name]) is not None:
                element.text = INVALID_XML.sub("\ufffd", value)
        XML.SubElement(stat, DAV + "status").text = status


def resource_href(request: Request, path: list[str], item: dict) -> str:
    root = request.scope.get("root_path", "").rstrip("/")
    encoded = "/".join(quote(name, safe="") for name in path)
    suffix = "/" if path and item["kind"] == "folder" else ""
    return f"{quote(root, safe='/')}/webdav/{encoded}{suffix}"


@router.api_route("", methods=METHODS, include_in_schema=False)
@router.api_route("/{path:path}", methods=METHODS, include_in_schema=False)
async def read_webdav(
    request: Request,
    db: Database = Depends(get_db),
    storage: TelegramStorage = Depends(get_storage),
) -> Response:
    if request.method == "OPTIONS":
        return Response(headers=HEADERS)
    chat_id = require_channel(storage)
    raw_path = request.path_params.get("path", "").removesuffix("/")
    path = raw_path.split("/") if raw_path else []
    item = resolve(db, path, chat_id)
    if request.method != "PROPFIND":
        if item["kind"] == "folder":
            return Response(headers=HEADERS)
        response = await stream_item(request, item, db, storage)
        response.headers.update(HEADERS)
        response.headers["Content-Disposition"] = content_disposition(item["name"], inline=False)
        response.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
        return response

    depth = request.headers.get("depth", "infinity")
    if depth == "infinity":
        error = XML.Element(DAV + "error")
        XML.SubElement(error, DAV + "propfind-finite-depth")
        return Response(XML.tostring(error), 403, headers=HEADERS, media_type="application/xml")
    if depth not in {"0", "1"}:
        raise HTTPException(400, "WebDAV Depth 无效")
    mode, requested = await requested_properties(request)
    root = XML.Element(DAV + "multistatus")
    add_properties(root, resource_href(request, path, item), item, mode, requested)
    if depth == "1" and item["kind"] == "folder":
        for name, child in members(db, item["id"], chat_id).items():
            add_properties(
                root, resource_href(request, [*path, name], child), child, mode, requested
            )
    return Response(
        XML.tostring(root, encoding="utf-8", xml_declaration=True),
        207,
        headers=HEADERS,
        media_type="application/xml",
    )
