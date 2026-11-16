from __future__ import annotations

import hmac
import mimetypes
from urllib.parse import quote, unquote, urlsplit
from xml.etree import ElementTree

import anyio
import httpx
from defusedxml import ElementTree as SafeXML
from defusedxml.common import DefusedXmlException
from fastapi import HTTPException

from app.config import Settings

DAV = "{DAV:}"
LIST_LIMIT = 8 * 1024 * 1024
INLINE_IMAGES = {"image/avif", "image/bmp", "image/gif", "image/jpeg", "image/png", "image/webp"}


def media_type(name: str) -> str:
    mime = mimetypes.guess_type(name)[0] or ""
    if mime in INLINE_IMAGES or mime.startswith(("video/", "audio/")):
        return mime
    return "application/octet-stream"


def valid_component(name: str) -> bool:
    return (
        bool(name)
        and name not in {".", ".."}
        and all(char not in "/\\" and ord(char) >= 32 and ord(char) != 127 for char in name)
    )


def path_components(path: str) -> tuple[str, ...]:
    parts = tuple(path.split("/")) if path else ()
    if not all(valid_component(part) for part in parts):
        raise HTTPException(400, "Google Drive 路径无效")
    return parts


def url_components(path: str) -> tuple[str, ...]:
    return tuple(unquote(part, errors="strict") for part in path.strip("/").split("/"))


def upstream_error(status: int) -> HTTPException:
    if status == 404:
        return HTTPException(404, "Google Drive 文件或目录不存在")
    if status in {401, 403}:
        return HTTPException(502, "Google Drive 挂载鉴权失败，请检查 OpenList 配置")
    if 300 <= status < 400:
        return HTTPException(502, "请将 OpenList 的 WebDAV 策略设置为本地代理")
    return HTTPException(502, "无法从 OpenList 读取 Google Drive")


async def close_response(response: httpx.Response) -> None:
    with anyio.CancelScope(shield=True):
        await response.aclose()


class GoogleDrive:
    def __init__(self, settings: Settings):
        self.base_url = httpx.URL(settings.google_drive_dav_url.rstrip("/") + "/")
        self.playback_key = settings.app_secret.encode()
        self.auth = httpx.BasicAuth(
            settings.google_drive_dav_username, settings.google_drive_dav_password
        )
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(45, connect=8), trust_env=False)

    async def close(self) -> None:
        await self.client.aclose()

    def url(self, parts: tuple[str, ...], *, directory: bool = False) -> httpx.URL:
        if not parts:
            return self.base_url
        encoded = "/".join(quote(part, safe="") for part in parts)
        suffix = b"/" if directory else b""
        return self.base_url.copy_with(raw_path=self.base_url.raw_path + encoded.encode() + suffix)

    async def open(self, method: str, url: httpx.URL, headers: dict[str, str]) -> httpx.Response:
        request = self.client.build_request(
            method, url, headers={"Accept-Encoding": "identity", **headers}
        )
        try:
            return await self.client.send(
                request, auth=self.auth, stream=True, follow_redirects=False
            )
        except httpx.TimeoutException:
            raise HTTPException(504, "连接 OpenList 超时") from None
        except httpx.RequestError:
            raise HTTPException(502, "无法连接 OpenList") from None

    async def list_folder(self, parts: tuple[str, ...]) -> list[dict]:
        url = self.url(parts, directory=True)
        response = await self.open("PROPFIND", url, {"Depth": "1"})
        try:
            if response.status_code != 207:
                raise upstream_error(response.status_code)
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > LIST_LIMIT:
                    raise HTTPException(502, "Google Drive 目录过大，请拆分文件夹")
        except httpx.RequestError:
            raise HTTPException(502, "无法读取 Google Drive 目录") from None
        finally:
            await close_response(response)
        try:
            root = SafeXML.fromstring(body, forbid_dtd=True)
        except (ElementTree.ParseError, DefusedXmlException, ValueError):
            raise HTTPException(502, "OpenList 返回了无效的目录") from None
        if root.tag != DAV + "multistatus":
            raise HTTPException(502, "OpenList 返回了无效的目录")
        parent = url_components(url.raw_path.decode())
        entries = {}
        for item in root.findall(DAV + "response"):
            try:
                child = url_components(urlsplit(item.findtext(DAV + "href", "")).path)
            except ValueError:
                continue
            if child[:-1] != parent or not valid_component(name := child[-1]):
                continue
            for stat in item.findall(DAV + "propstat"):
                status = stat.findtext(DAV + "status", "").split()
                props = stat.find(DAV + "prop")
                if len(status) < 2 or status[1] != "200" or props is None:
                    continue
                folder = props.find(f"{DAV}resourcetype/{DAV}collection") is not None
                size = props.findtext(DAV + "getcontentlength", "0")
                item_url = self.url((*parts, name))
                entries[name] = {
                    "id": hmac.digest(self.playback_key, str(item_url).encode(), "sha256").hex(),
                    "name": name,
                    "folder": folder,
                    "size": int(size)
                    if size.isascii() and size.isdecimal() and len(size) <= 18
                    else 0,
                    "mime": None if folder else media_type(name),
                }
                break
        return sorted(
            entries.values(), key=lambda item: (not item["folder"], item["name"].casefold())
        )
