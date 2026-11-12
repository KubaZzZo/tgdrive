from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from telethon.errors import FloodWaitError, MessageDeleteForbiddenError, MessageIdInvalidError

from app.db import Database
from app.storage import TelegramStorage

logger = logging.getLogger("tgdrive.telegram_cleanup")


class TelegramCleanup:
    def __init__(self, db: Database, storage: TelegramStorage):
        self.db = db
        self.storage = storage
        self._lock = asyncio.Lock()
        self._worker: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run(), name="telegram-cleanup")

    async def stop(self) -> None:
        worker, self._worker = self._worker, None
        if worker:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            try:
                await self.process()
            except Exception:
                logger.exception("Telegram cleanup worker failed")
            await asyncio.sleep(15)

    async def process(self, batch_id: str | None = None) -> None:
        if self.storage.chat is None:
            return
        chat_id = str(self.storage.chat)
        while True:
            async with self._lock:
                rows = self.db.due_telegram_cleanup(chat_id, time.time(), batch_id)
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
            self.db.defer_telegram_cleanup(
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
        self.db.finish_telegram_cleanup(chat_id, message_ids)
        return True
