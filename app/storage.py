from __future__ import annotations

import asyncio
import io
import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio
from telethon import TelegramClient, events, utils
from telethon.tl.types import DocumentAttributeFilename

from app.config import Settings
from app.config import settings as default_settings
from app.network import get_proxy

ProgressCallback = Callable[[int, int], Awaitable[None] | None]
logger = logging.getLogger("tgdrive.storage")


class SegmentReader(io.RawIOBase):
    """A read-only view over a byte range of a file.

    Telethon's uploader consumes a stream sequentially and we pass the exact
    size separately, so this keeps memory usage bounded even for multi-GB
    videos.
    """

    def __init__(self, path: Path, offset: int, size: int, name: str):
        super().__init__()
        self._path = path
        self._offset = offset
        self._size = size
        self._pos = 0
        self.name = name
        self._fh = open(path, "rb")  # noqa: SIM115 - lifetime owned by SegmentReader
        self._fh.seek(offset)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def tell(self) -> int:
        return self._pos

    def read(self, n: int = -1) -> bytes:
        remaining = self._size - self._pos
        if remaining <= 0:
            return b""
        if n is None or n < 0 or n > remaining:
            n = remaining
        data = self._fh.read(n)
        self._pos += len(data)
        return data

    def close(self) -> None:
        try:
            self._fh.close()
        finally:
            super().close()


class TelegramStorage:
    def __init__(self, settings: Settings = default_settings):
        self.settings = settings
        self.client: TelegramClient | None = None
        self.chat: int | str | None = None
        self.error: str | None = None
        self._started = False
        self._message_cache: OrderedDict[tuple[str, int], tuple[float, Any]] = OrderedDict()

    @property
    def mode(self) -> str:
        return self.settings.tg_mode

    @property
    def configured(self) -> bool:
        return self.settings.is_configured

    @staticmethod
    def _normalize_chat(value: str) -> int | str:
        value = value.strip()
        if value.lstrip("-").isdigit():
            return int(value)
        return value

    def _session_path(self) -> str:
        suffix = "_bot" if self.mode == "bot" else ""
        return str(self.settings.tg_session) + suffix

    async def start(self) -> None:
        if self._started:
            return
        if not self.settings.tg_api_id or not self.settings.tg_api_hash:
            self.error = "缺少 TG_API_ID / TG_API_HASH"
            return
        if not self.settings.tg_chat_id:
            self.error = "缺少 TG_CHAT_ID（私有频道 ID）"
            return

        self.chat = self._normalize_chat(self.settings.tg_chat_id)
        kwargs = {
            "connection_retries": 5,
            "request_retries": 5,
            "timeout": 60,
            "flood_sleep_threshold": 60,
            "proxy": get_proxy(),
        }

        try:
            if self.mode == "bot":
                if not self.settings.tg_bot_token:
                    self.error = "缺少 TG_BOT_TOKEN"
                    return
                self.client = TelegramClient(
                    self._session_path(),
                    self.settings.tg_api_id,
                    self.settings.tg_api_hash,
                    **kwargs,
                )
                await self.client.start(bot_token=self.settings.tg_bot_token)
            else:
                self.client = TelegramClient(
                    self._session_path(),
                    self.settings.tg_api_id,
                    self.settings.tg_api_hash,
                    **kwargs,
                )
                await self.client.connect()
                if not await self.client.is_user_authorized():
                    self.error = "Telegram session 未登录，请先运行: python scripts/login_qr.py"
                    await self.client.disconnect()
                    self.client = None
                    return

            # Validate that the configured chat is actually reachable.
            entity = await self.client.get_entity(self.chat)
            self.chat = utils.get_peer_id(entity)
            me = await self.client.get_me()
            self.error = None
            self._started = True
            print(
                f"[telegram] connected mode={self.mode} account={getattr(me, 'username', None) or getattr(me, 'id', '?')}"
            )
        except Exception as exc:  # pragma: no cover - depends on live Telegram
            self.error = f"Telegram 连接失败: {exc}"
            if self.client:
                try:
                    await self.client.disconnect()
                except Exception:
                    pass
            self.client = None

    async def stop(self) -> None:
        if self.client:
            await self.client.disconnect()
        self.client = None
        self._started = False

    def require_client(self) -> TelegramClient:
        if not self._started or not self.client or self.chat is None:
            raise RuntimeError(self.error or "Telegram 尚未连接")
        return self.client

    async def me(self) -> dict[str, Any] | None:
        client = self.require_client()
        me = await client.get_me()
        if not me:
            return None
        return {
            "id": me.id,
            "username": getattr(me, "username", None),
            "first_name": getattr(me, "first_name", None),
            "bot": bool(getattr(me, "bot", False)),
        }

    @staticmethod
    async def _progress(cb: ProgressCallback | None, sent: int, total: int) -> None:
        if cb is None:
            return
        result = cb(sent, total)
        if asyncio.iscoroutine(result):
            await result

    async def upload_file(
        self,
        path: Path,
        name: str,
        mime: str | None = None,
        progress: ProgressCallback | None = None,
        storage_tag: str | None = None,
    ) -> list[dict[str, Any]]:
        """Upload a file, splitting it into Telegram-safe segments.

        Telegram's MTProto uploader transfers each message in 512 KiB parts.
        On top of that, files larger than TG_SEGMENT_SIZE are split into
        independent documents, keeping a failed upload recoverable at segment
        granularity.  Messages created by a failed attempt are removed on a
        best-effort basis so normal failures do not leave orphan documents.
        """
        client = self.require_client()
        assert self.chat is not None
        total_size = path.stat().st_size
        if total_size <= 0:
            raise ValueError("空文件暂不支持")

        segment_size = max(1, int(self.settings.tg_segment_size))
        segment_count = (total_size + segment_size - 1) // segment_size
        tag = storage_tag or uuid.uuid4().hex
        parts: list[dict[str, Any]] = []
        uploaded_message_ids: list[int] = []

        try:
            for index in range(segment_count):
                start = index * segment_size
                size = min(segment_size, total_size - start)
                part_name = name if segment_count == 1 else f"{name}.part{index + 1:03d}"
                reader = SegmentReader(path, start, size, part_name)

                async def part_progress(sent: int, part_total: int, *, _start: int = start) -> None:
                    await self._progress(progress, _start + sent, total_size)

                try:
                    uploaded = await client.upload_file(
                        reader,
                        file_size=size,
                        part_size_kb=512,
                        file_name=part_name,
                        progress_callback=part_progress,
                    )
                    message = await client.send_file(
                        self.chat,
                        uploaded,
                        caption=f"tgdrive:v1:{tag}:{index}",
                        force_document=True,
                        attributes=[DocumentAttributeFilename(part_name)],
                    )
                finally:
                    reader.close()

                if not message:
                    raise RuntimeError(f"Telegram 未返回第 {index + 1} 段的消息")
                uploaded_message_ids.append(int(message.id))
                parts.append(
                    {
                        "part_index": index,
                        "tg_message_id": int(message.id),
                        "size": size,
                    }
                )
            return parts
        except (Exception, asyncio.CancelledError):
            if uploaded_message_ids:
                try:
                    await self.delete_messages(uploaded_message_ids)
                    logger.info("Deleted %s partial Telegram segment(s)", len(uploaded_message_ids))
                except Exception:
                    logger.exception("Failed to clean up partial Telegram segments")
            raise

    async def _get_message(self, message_id: int) -> Any:
        client = self.require_client()
        assert self.chat is not None
        key = (str(self.chat), message_id)
        cached = self._message_cache.get(key)
        if cached and time.monotonic() - cached[0] < 300:
            self._message_cache.move_to_end(key)
            return cached[1]
        message = await client.get_messages(self.chat, ids=message_id)
        if not message:
            raise FileNotFoundError(f"Telegram 消息不存在: {message_id}")
        self._message_cache[key] = (time.monotonic(), message)
        self._message_cache.move_to_end(key)
        while len(self._message_cache) > 256:
            self._message_cache.popitem(last=False)
        return message

    async def iter_download(self, message_id: int, offset: int, length: int):
        client = self.require_client()
        message = await self._get_message(message_id)
        if not getattr(message, "document", None) and not getattr(message, "photo", None):
            raise FileNotFoundError(f"Telegram 消息不是文件: {message_id}")
        if length <= 0:
            return
        # Telegram requires a 4 KiB multiple that divides 1 MiB; use powers of two.
        # Telethon limit counts chunks, while the final output is clipped to bytes.
        chunk_size = min(512 * 1024, max(4096, 1 << (length - 1).bit_length()))
        stream = client.iter_download(
            message,
            offset=offset,
            limit=(length + chunk_size - 1) // chunk_size,
            chunk_size=chunk_size,
            request_size=chunk_size,
        )
        remaining = length
        try:
            async for chunk in stream:
                data = bytes(chunk[:remaining])
                remaining -= len(data)
                if data:
                    yield data
                if remaining == 0:
                    break
            if remaining:
                raise OSError("Telegram 文件内容不完整")
        finally:
            # Seek/disconnect cancellation must still return borrowed Telegram senders.
            with anyio.CancelScope(shield=True):
                await stream.close()

    def invalidate_message(self, message_id: int) -> None:
        self._message_cache.pop((str(self.chat), message_id), None)

    async def latest_channel_message_id(self) -> int:
        messages = await self.require_client().get_messages(self.chat, limit=1)
        return int(messages[0].id) if messages else 0

    async def iter_channel_messages(self, after_id: int, max_id: int):
        if max_id <= after_id:
            return
        async for message in self.require_client().iter_messages(
            self.chat,
            min_id=after_id,
            max_id=max_id + 1,
            reverse=True,
            limit=None,
        ):
            yield message

    async def subscribe_channel(self, on_message, on_edit, on_delete):
        client = self.require_client()

        async def added(event):
            await on_message(event.message)

        async def edited(event):
            await on_edit(event.message)

        async def deleted(event):
            await on_delete(event.deleted_ids)

        handlers = [
            (added, events.NewMessage(chats=self.chat)),
            (edited, events.MessageEdited(chats=self.chat)),
            (deleted, events.MessageDeleted(chats=self.chat)),
        ]
        for callback, builder in handlers:
            client.add_event_handler(callback, builder)

        def unsubscribe():
            for callback, builder in handlers:
                client.remove_event_handler(callback, builder)

        return unsubscribe

    async def delete_messages(self, message_ids: list[int]) -> None:
        if not message_ids:
            return
        client = self.require_client()
        assert self.chat is not None
        for i in range(0, len(message_ids), 100):
            batch = message_ids[i : i + 100]
            await client.delete_messages(self.chat, batch)
            for message_id in batch:
                self._message_cache.pop((str(self.chat), message_id), None)

    def status(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "connected": self._started,
            "mode": self.mode,
            "error": self.error,
            "chat_id": str(self.chat) if self.chat is not None else None,
            "segment_size": self.settings.tg_segment_size,
        }
