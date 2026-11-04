from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto

from app.categories import effective_mime
from app.config import Settings
from app.db import Database
from app.storage import TelegramStorage

logger = logging.getLogger("tgdrive.sync")


def describe_message(message: Any) -> dict[str, Any] | None:
    if (getattr(message, "message", "") or "").startswith("tgdrive:v1:"):
        return None
    if not isinstance(getattr(message, "media", None), (MessageMediaDocument, MessageMediaPhoto)):
        return None
    file = message.file
    if not file:
        return None
    media = message.document or message.photo
    size = file.size
    if message.photo:
        # Match Telethon iter_download's selected (last) photo size exactly.
        sizes = getattr(message.photo, "sizes", [])
        if not sizes:
            return None
        photo_size = sizes[-1]
        size = (
            getattr(photo_size, "size", 0)
            or max(getattr(photo_size, "sizes", []) or [0])
            or len(getattr(photo_size, "bytes", b""))
        )
    if not size or size <= 0:
        return None
    name = (
        file.name or f"{'照片' if message.photo else '频道文件'}_{message.id}{file.ext or '.bin'}"
    )
    name = re.sub(r"[\\/\x00-\x1f]", "_", name).strip(" .")[:255] or f"文件_{message.id}.bin"
    date = message.date or datetime.now(UTC)
    return {
        "name": name,
        "size": int(size),
        "mime": effective_mime(name, file.mime_type),
        "media_key": f"{'document' if message.document else 'photo'}:{media.id}",
        "remote_version": (message.edit_date or date).astimezone(UTC).isoformat(),
        "created_at": date.astimezone(UTC).isoformat(),
    }


class ChannelSyncService:
    def __init__(self, db: Database, storage: TelegramStorage, settings: Settings):
        self.db, self.storage, self.settings = db, storage, settings
        self._worker: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._full = True
        self._unsubscribe = None
        self.state = "disabled" if not settings.channel_sync_enabled else "unavailable"
        self.running = False
        self.scanned = 0
        self.imported = 0
        self.revision = 0
        self.last_error: str | None = None
        self.last_success: str | None = None

    @property
    def chat_id(self) -> str:
        return str(self.storage.chat)

    async def start(self) -> None:
        if (
            not self.settings.channel_sync_enabled
            or not self.storage.status()["connected"]
            or not hasattr(self.storage, "subscribe_channel")
        ):
            return
        self._unsubscribe = await self.storage.subscribe_channel(
            self.on_message, self.on_edit, self.on_delete
        )
        self.state = "idle"
        self._wake.set()
        self._worker = asyncio.create_task(self._loop(), name="channel-sync")

    def request_scan(self, full: bool = False) -> bool:
        if not self._worker or self._worker.done():
            return False
        if self.running and not full:
            return False
        self._full = self._full or full
        self._wake.set()
        return True

    def ingest(self, message: Any, scan_run: str | None = None) -> bool:
        file = describe_message(message)
        if not file:
            return False
        changed = self.db.import_channel_file(self.chat_id, int(message.id), file, scan_run)
        if changed:
            self.storage.invalidate_message(int(message.id))
            self.revision += 1
            self.imported += 1
        return changed

    async def on_message(self, message: Any) -> None:
        try:
            self.ingest(message)
        except Exception as exc:
            logger.exception("Could not import channel message")
            self.last_error = str(exc)
            self._full = True
            self._wake.set()

    async def on_edit(self, message: Any) -> None:
        if describe_message(message):
            await self.on_message(message)
        else:
            await self.on_delete([int(message.id)])

    async def on_delete(self, ids: list[int]) -> None:
        for mid in ids:
            self.storage.invalidate_message(mid)
        self.revision += self.db.remove_channel_messages(self.chat_id, ids)

    async def scan(self, full: bool = False) -> None:
        self.running = True
        self.state = "syncing"
        self.scanned = 0
        self.last_error = None
        generation = uuid.uuid4().hex if full else None
        try:
            max_id = max(
                await self.storage.latest_channel_message_id(),
                self.db.channel_cursor(self.chat_id),
                self.db.channel_high_water(self.chat_id),
            )
            cursor = 0 if full else self.db.channel_cursor(self.chat_id)
            async for message in self.storage.iter_channel_messages(cursor, max_id):
                self.ingest(message, generation)
                self.db.advance_channel_cursor(self.chat_id, int(message.id))
                self.scanned += 1
                if self.scanned % 100 == 0:
                    await asyncio.sleep(0)
            if full:
                self.revision += self.db.reconcile_channel(self.chat_id, generation, max_id)
            self.db.advance_channel_cursor(self.chat_id, max_id)
            self.last_success = datetime.now(UTC).isoformat()
            self.state = "idle"
        except Exception as exc:
            self.state = "error"
            self.last_error = str(exc)
            logger.exception("Channel synchronization failed")
            raise
        finally:
            self.running = False

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=self.settings.channel_sync_interval
                )
            except TimeoutError:
                pass
            self._wake.clear()
            full, self._full = self._full, False
            try:
                await self.scan(full)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._full = self._full or full

    async def shutdown(self) -> None:
        if self._unsubscribe:
            self._unsubscribe()
        if self._worker:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        self.state = "stopped"

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.channel_sync_enabled,
            "state": self.state,
            "running": self.running or self._wake.is_set(),
            "scanned": self.scanned,
            "imported": self.imported,
            "files": self.db.channel_file_count(self.chat_id),
            "revision": self.revision,
            "last_success": self.last_success,
            "error": self.last_error,
            "interval": self.settings.channel_sync_interval,
        }
