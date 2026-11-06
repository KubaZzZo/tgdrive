from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import httpx

MAX_BOT_DOWNLOAD = 20 * 1024 * 1024
BOT_API = "https://api.telegram.org"


class BotAPIError(RuntimeError):
    def __init__(self, message: str, retry_after: int = 0):
        super().__init__(message)
        self.retry_after = retry_after


class TelegramBot:
    def __init__(self, token: str):
        self._token = token
        self.valid_token = bool(re.fullmatch(r"[0-9]+:[a-zA-Z0-9_-]+", token))
        self.id = token.partition(":")[0] if self.valid_token else ""

    async def call(self, method: str, **data: Any) -> Any:
        if not self.valid_token:
            raise BotAPIError("请检查 TG_BOT_FALLBACK_TOKEN 的格式。")
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(f"{BOT_API}/bot{self._token}/{method}", json=data)
                result = response.json()
        except (httpx.HTTPError, ValueError):
            raise BotAPIError("无法连接 Telegram Bot API，稍后重试。") from None
        if result.get("ok"):
            return result["result"]
        if response.status_code == 401:
            raise BotAPIError("Telegram 拒绝了 Bot Token，请检查备用 Bot 配置。")
        if method == "getChatMember" and response.status_code in {400, 403}:
            raise BotAPIError("备用 Bot 无法访问当前频道，请将它添加为频道管理员。")
        if response.status_code == 409:
            raise BotAPIError("备用 Bot 已有其他轮询服务或 webhook，请为 TG Drive 使用专用 Bot。")
        retry_after = result.get("parameters", {}).get("retry_after", 0)
        raise BotAPIError("Telegram Bot 请求暂不可用，稍后重试。", retry_after)

    async def check_channel(self, chat_id: int) -> None:
        member = await self.call("getChatMember", chat_id=chat_id, user_id=int(self.id or 0))
        if member.get("status") not in {"creator", "administrator", "member"}:
            raise BotAPIError("备用 Bot 未加入当前频道，请将它添加为频道管理员。")

    async def updates(self, offset: int | None) -> list[dict]:
        params: dict[str, Any] = {"timeout": 20, "allowed_updates": ["channel_post"]}
        if offset is not None:
            params["offset"] = offset
        return await self.call("getUpdates", **params)

    async def read(self, file_id: str, size: int, start: int, end: int) -> tuple[bytes, int]:
        file = await self.call("getFile", file_id=file_id)
        path = file.get("file_path")
        if not path:
            raise BotAPIError("Telegram 未提供备用下载地址。")
        headers = {"Range": f"bytes={start}-{end - 1}"} if end - start < size else {}
        try:
            async with (
                httpx.AsyncClient(timeout=30) as client,
                client.stream(
                    "GET", f"{BOT_API}/file/bot{self._token}/{quote(path)}", headers=headers
                ) as response,
            ):
                partial = response.status_code == 206
                expected = end - start if partial else size
                if (
                    partial
                    and response.headers.get("Content-Range") != f"bytes {start}-{end - 1}/{size}"
                ):
                    raise BotAPIError("Telegram Bot 返回了错误的文件范围。")
                if response.status_code not in {200, 206}:
                    raise BotAPIError("Telegram Bot 下载失败，请稍后重试。")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > expected:
                        raise BotAPIError("Telegram Bot 返回的文件大小不符。")
                    data.extend(chunk)
        except httpx.HTTPError:
            raise BotAPIError("Telegram Bot 下载失败，请稍后重试。") from None
        if len(data) != expected:
            raise BotAPIError("Telegram Bot 返回的文件大小不符。")
        return (bytes(data) if partial else bytes(data[start:end])), len(data)
