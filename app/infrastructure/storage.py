from __future__ import annotations

import asyncio
import io
import logging
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from pathlib import Path
from typing import Any

import anyio
from telethon import TelegramClient, events, utils
from telethon.errors import (
    AuthKeyDuplicatedError,
    AuthKeyInvalidError,
    AuthKeyUnregisteredError,
    SessionExpiredError,
    SessionRevokedError,
)
from telethon.tl.types import DocumentAttributeFilename

from app.config import Settings
from app.config import settings as default_settings
from app.core.network import get_proxy
from app.infrastructure.telegram_upload import (
    PARALLEL_UPLOAD_MIN_BYTES,
    UPLOAD_PART_BYTES,
    upload_parallel,
)

ProgressCallback = Callable[[int, int], Awaitable[None] | None]
logger = logging.getLogger("tgdrive.storage")
DOWNLOAD_BLOCK_SIZE = 512 * 1024
PREFETCH_MIN_BYTES = 2 * 1024 * 1024
PREFETCH_DEPTH = 4
PREFETCH_DOWNLOAD_LIMIT = 2
SESSION_AUTH_ERRORS = (
    AuthKeyDuplicatedError,
    AuthKeyInvalidError,
    AuthKeyUnregisteredError,
    SessionExpiredError,
    SessionRevokedError,
)


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
        self.auth_required = False
        self._started = False
        self.traffic = None
        self._message_cache: OrderedDict[tuple[str, int], tuple[float, Any]] = OrderedDict()
        self._prefetch_downloads = 0

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
        if self.settings.tg_chat_id:
            self.chat = self._normalize_chat(self.settings.tg_chat_id)
        if not self.settings.tg_api_id or not self.settings.tg_api_hash:
            self.error = "缺少 TG_API_ID / TG_API_HASH"
            return
        if not self.settings.tg_chat_id:
            self.error = "缺少 TG_CHAT_ID（私有频道 ID）"
            return

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

            me = await self.client.get_me()
            if not me:
                self.invalidate_session()
                return
            # Validate that the configured chat is actually reachable.
            entity = await self.client.get_entity(self.chat)
            self.chat = utils.get_peer_id(entity)
            self.auth_required = False
            self.error = None
            self._started = True
            logger.info(
                "Telegram connected mode=%s account=%s",
                self.mode,
                getattr(me, "username", None) or getattr(me, "id", "?"),
            )
        except SESSION_AUTH_ERRORS:
            self.invalidate_session()
        except Exception as exc:  # pragma: no cover - depends on live Telegram
            self.error = f"Telegram 连接失败: {exc}"
        finally:
            if self.client and not self._started:
                try:
                    await self.client.disconnect()
                except Exception:
                    logger.warning("Could not disconnect failed Telegram client", exc_info=True)
                self.client = None

    def invalidate_session(self) -> None:
        self.auth_required = True
        self._started = False
        self.error = "Telegram 会话已失效，请在「Telegram 账号」中重新扫码并选择原存储频道。"

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
                last_sent = 0

                async def part_progress(sent: int, part_total: int, *, _start: int = start) -> None:
                    nonlocal last_sent
                    if self.traffic and sent > last_sent:
                        self.traffic.record("telegram", outbound=sent - last_sent)
                    last_sent = max(last_sent, sent)
                    await self._progress(progress, _start + sent, total_size)

                try:
                    connections = self.settings.tg_upload_connections
                    parallel = connections > 1 and size >= PARALLEL_UPLOAD_MIN_BYTES
                    logger.info(
                        "Upload %s segment=%d bytes=%d telegram_connections=%d",
                        tag,
                        index,
                        size,
                        connections if parallel else 1,
                    )
                    if parallel:
                        uploaded = await upload_parallel(
                            client, reader, size, part_name, connections, part_progress
                        )
                    else:
                        uploaded = await client.upload_file(
                            reader,
                            file_size=size,
                            part_size_kb=UPLOAD_PART_BYTES // 1024,
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

    async def iter_download(
        self, message_id: int, offset: int, length: int, *, prefetch: bool = False
    ):
        if (
            prefetch
            and length >= PREFETCH_MIN_BYTES
            and self._prefetch_downloads < PREFETCH_DOWNLOAD_LIMIT
        ):
            self._prefetch_downloads += 1
            try:
                async with aclosing(self._prefetch_download(message_id, offset, length)) as stream:
                    async for chunk in stream:
                        yield chunk
            finally:
                self._prefetch_downloads -= 1
            return

        async with aclosing(self._retry_download(message_id, offset, length)) as stream:
            async for chunk in stream:
                yield chunk

    async def _retry_download(self, message_id: int, offset: int, length: int, message=None):
        remaining = length
        for attempt in range(3):
            try:
                async with aclosing(
                    self._iter_download_once(message_id, offset, remaining, message)
                ) as stream:
                    async for chunk in stream:
                        offset += len(chunk)
                        remaining -= len(chunk)
                        yield chunk
                return
            except FileNotFoundError:
                raise
            except OSError:
                if attempt == 2:
                    raise
                logger.warning("Retrying Telegram download %s at byte %s", message_id, offset)
                await asyncio.sleep(0.25 * 2**attempt)

    async def _prefetch_download(self, message_id: int, offset: int, length: int):
        try:
            message = await self._get_message(message_id)
        except FileNotFoundError:
            raise
        except OSError:
            async with aclosing(self._retry_download(message_id, offset, length)) as stream:
                async for chunk in stream:
                    yield chunk
            return

        end = offset + length
        pending: deque[asyncio.Task[bytes]] = deque()

        async def read_block(start: int, size: int) -> bytes:
            async with aclosing(self._retry_download(message_id, start, size, message)) as stream:
                return b"".join([chunk async for chunk in stream])

        def schedule() -> None:
            nonlocal offset
            size = min(end - offset, DOWNLOAD_BLOCK_SIZE - offset % DOWNLOAD_BLOCK_SIZE)
            pending.append(asyncio.create_task(read_block(offset, size)))
            offset += size

        try:
            # Send the first block without competing with later blocks.
            schedule()
            while pending:
                chunk = await pending[0]
                pending.popleft()
                while len(pending) < PREFETCH_DEPTH and offset < end:
                    schedule()
                yield chunk
        finally:
            with anyio.CancelScope(shield=True):
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

    async def _iter_download_once(self, message_id: int, offset: int, length: int, message=None):
        client = self.require_client()
        if message is None:
            message = await self._get_message(message_id)
        if not getattr(message, "document", None) and not getattr(message, "photo", None):
            raise FileNotFoundError(f"Telegram 消息不是文件: {message_id}")
        if length <= 0:
            return
        # Telegram requires a 4 KiB multiple that divides 1 MiB; use powers of two.
        # Align the start ourselves: Telethon can select its direct iterator for
        # a 4 KiB-aligned offset even when a request would cross a 1 MiB boundary.
        # Browser ranges such as 688128-1048575 trigger that path.
        chunk_size = min(DOWNLOAD_BLOCK_SIZE, max(4096, 1 << (length - 1).bit_length()))
        aligned_offset = offset // chunk_size * chunk_size
        skip = offset - aligned_offset
        stream = client.iter_download(
            message,
            offset=aligned_offset,
            limit=(skip + length + chunk_size - 1) // chunk_size,
            chunk_size=chunk_size,
            request_size=chunk_size,
        )
        remaining = length
        try:
            async for chunk in stream:
                if self.traffic:
                    self.traffic.record("telegram", inbound=len(chunk))
                if skip:
                    discarded = min(skip, len(chunk))
                    chunk = chunk[discarded:]
                    skip -= discarded
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

    async def thumbnail(self, message_id: int) -> bytes | None:
        """Fetch a static Telegram thumbnail into memory, never onto the VPS disk."""
        message = await self._get_message(message_id)
        sizes = getattr(getattr(message, "document", None), "thumbs", None) or []
        # Do not accidentally download an animated/video thumbnail.
        sizes = [
            s
            for s in sizes
            if type(s).__name__ in {"PhotoSize", "PhotoCachedSize", "PhotoSizeProgressive"}
        ]
        if not sizes:
            return None
        data = await self.require_client().download_media(message, bytes, thumb=sizes[-1])
        if self.traffic and data:
            self.traffic.record("telegram", inbound=len(data))
        return data or None

    async def latest_channel_message_id(self) -> int:
        messages = await self.require_client().get_messages(self.chat, limit=1)
        return int(messages[0].id) if messages else 0

    async def get_album_messages(self, message: Any) -> list[Any]:
        group = getattr(message, "grouped_id", None)
        if not group:
            return [message]
        # Telegram albums contain at most 10 media. Fetch a bounded neighborhood
        # and require the exact group ID; proximity alone never links captions.
        messages = await self.require_client().get_messages(
            self.chat, ids=list(range(max(1, message.id - 16), message.id + 17))
        )
        return [m for m in messages if m and getattr(m, "grouped_id", None) == group]

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

        async def album(event):
            if event.messages:
                # Refresh after all members arrive, including caption-first or
                # caption-last albums split across Telegram update packets.
                await on_message(event.messages[0])

        handlers = [
            (added, events.NewMessage(chats=self.chat)),
            (edited, events.MessageEdited(chats=self.chat)),
            (deleted, events.MessageDeleted(chats=self.chat)),
            (album, events.Album(chats=self.chat)),
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
            "auth_required": self.auth_required,
            "mode": self.mode,
            "error": self.error,
            "chat_id": str(self.chat) if self.chat is not None else None,
            "segment_size": self.settings.tg_segment_size,
            "upload_connections": self.settings.tg_upload_connections,
        }
