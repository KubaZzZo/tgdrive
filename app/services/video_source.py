from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import PurePosixPath
from urllib.parse import unquote

import httpx

from app.infrastructure.db.common import clean_name

VIDEO_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg",
    ".ts": "video/mp2t",
}


def validate_url(value: str) -> httpx.URL:
    try:
        url = httpx.URL(value)
        port = url.port or (443 if url.scheme == "https" else 80)
        if (
            url.scheme in {"http", "https"}
            and url.host
            and not url.userinfo
            and port == (443 if url.scheme == "https" else 80)
        ):
            return url.copy_with(fragment=None)
    except (httpx.InvalidURL, ValueError):
        pass
    raise ValueError("请输入不含账号凭据的 HTTP(S) 直链，使用默认端口")


async def public_address(url: httpx.URL) -> str:
    origin = url.raw_host.decode("ascii")
    port = 443 if url.scheme == "https" else 80
    addresses = await asyncio.get_running_loop().getaddrinfo(origin, port, type=socket.SOCK_STREAM)
    if not addresses:
        raise ValueError("下载域名无法解析")
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if (
            not ip.is_global
            or ip.is_multicast
            or ip.is_reserved
            or (
                isinstance(ip, ipaddress.IPv6Address)
                and (
                    ip.ipv4_mapped is not None
                    or ip.sixtofour is not None
                    or ip.teredo is not None
                    or ip in ipaddress.ip_network("64:ff9b::/96")
                )
            )
        ):
            raise ValueError("下载链接必须指向公网地址")
    return addresses[0][4][0]


@asynccontextmanager
async def open_video(url: httpx.URL) -> AsyncIterator[tuple[httpx.Response, httpx.URL]]:
    for _ in range(6):
        address = await public_address(url)
        host = url.raw_host.decode("ascii")
        authority = f"[{host}]" if ":" in host else host
        # Pin the connection to the validated address while preserving Host and TLS identity.
        # Each hop gets its own pool so different origins sharing an IP cannot reuse TLS.
        async with (
            httpx.AsyncClient(trust_env=False, timeout=30) as client,
            client.stream(
                "GET",
                url.copy_with(host=address),
                headers={"Host": authority, "Accept-Encoding": "identity"},
                extensions={"sni_hostname": host},
            ) as response,
        ):
            if response.is_redirect and (location := response.headers.get("location")):
                url = validate_url(str(url.join(location)))
                continue
            if (
                response.status_code != 200
                or response.headers.get("content-encoding", "identity").lower() != "identity"
            ):
                raise ValueError("远端未提供可直接下载的完整视频")
            yield response, url
            return
    raise ValueError("下载链接重定向次数过多")


def video_metadata(url: httpx.URL, response: httpx.Response, index: int) -> tuple[str, str]:
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    name = unquote(PurePosixPath(url.path).name)
    suffix = PurePosixPath(name).suffix.lower()
    if content_type.startswith("video/"):
        extension = next(
            (ext for ext, mime in VIDEO_TYPES.items() if mime == content_type), ".video"
        )
        if suffix not in VIDEO_TYPES:
            name = f"video-{index}{extension}"
    elif content_type in {"", "application/octet-stream"} and suffix in VIDEO_TYPES:
        content_type = VIDEO_TYPES[suffix]
    else:
        raise ValueError("链接需要直接返回视频文件")
    try:
        name = clean_name(name)
    except ValueError:
        name = f"video-{index}{suffix}"
    return name, content_type
