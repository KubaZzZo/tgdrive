from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
from telethon.errors import FloodWaitError, MessageDeleteForbiddenError, MessageIdInvalidError

from app.infrastructure.db import Database
from app.infrastructure.storage import TelegramStorage

logger = logging.getLogger("tgdrive.telegram_cleanup")
TRASH_RETENTION_DAYS = 30


class TelegramCleanup:
    def __init__(self, db: Database, storage: TelegramStorage):
        self.db = db
        self.storage = storage
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run(), name="telegram-cleanup")

    async def stop(self) -> None:
        worker, self._worker = self._worker, None
        if worker:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    def wake(self) -> None:
        self._wake.set()

    async def _run(self) -> None:
        while True:
            try:
                self._wake.clear()
                cutoff = datetime.now(UTC) - timedelta(days=TRASH_RETENTION_DAYS)
                purge = asyncio.create_task(
                    asyncio.to_thread(self.purge_trash, cutoff.isoformat(), single_batch=True)
                )
                try:
                    await asyncio.shield(purge)
                except asyncio.CancelledError:
                    await asyncio.gather(purge, return_exceptions=True)
                    raise
                await self.process()
            except Exception:
                logger.exception("Telegram cleanup worker failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=15)
            except TimeoutError:
                pass

    def purge_trash(self, cutoff: str, *, single_batch: bool = False) -> int:
        chat = self.storage.chat
        chat_id = str(chat) if chat is not None else ""
        deleted = 0
        while ids := self.db.trash_ids(cutoff, 50):
            for item_id in ids:
                result = self.db.delete_tree(
                    item_id, fallback_chat_id=chat_id, trashed_before=cutoff
                )
                deleted += result["deleted"]
            if single_batch:
                break
        return deleted

    async def process(self, batch_id: str | None = None) -> None:
        if self.storage.chat is None:
            return
        chat_id = str(self.storage.chat)
        while True:
            async with self._lock:
                rows = await anyio.to_thread.run_sync(
                    self.db.due_telegram_cleanup, chat_id, time.time(), batch_id
                )
                if not rows:
                    return
                if not await self._delete_batch(chat_id, rows):
                    return

    async def _delete_batch(self, chat_id: str, rows: list[dict[str, Any]]) -> bool:
        message_ids = [row["message_id"] for row in rows]
        try:
            status = self.storage.status()
            if not status["connected"]:
                raise RuntimeError(status.get("error") or "Telegram 尚未连接")
            await asyncio.wait_for(self.storage.delete_messages(message_ids), timeout=60)
        except Exception as exc:
            message_error = isinstance(exc, (MessageDeleteForbiddenError, MessageIdInvalidError))
            if message_error and len(rows) > 1:
                middle = len(rows) // 2
                return await self._delete_batch(
                    chat_id, rows[:middle]
                ) and await self._delete_batch(chat_id, rows[middle:])
            logger.warning("Telegram cleanup failed for channel %s: %s", chat_id, exc)
            now = time.time()
            error = f"{type(exc).__name__}: {exc}"[:1000]
            flood_wait = exc.seconds if isinstance(exc, FloodWaitError) else 0
            await anyio.to_thread.run_sync(
                self.db.defer_telegram_cleanup,
                chat_id,
                [
                    (
                        now + max(flood_wait, min(3600, 30 * 2 ** min(row["attempts"], 7))),
                        error,
                        row["message_id"],
                    )
                    for row in rows
                ],
            )
            return message_error
        await anyio.to_thread.run_sync(self.db.finish_telegram_cleanup, chat_id, message_ids)
        return True
