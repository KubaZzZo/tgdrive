from __future__ import annotations

from typing import Any

from app.infrastructure.db.session import SQLiteSession


class CleanupRepository(SQLiteSession):
    def due_telegram_cleanup(
        self, chat_id: str, now: float, batch_id: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM telegram_cleanup WHERE chat_id=? AND retry_at<=?"
        params: list[Any] = [chat_id, now]
        if batch_id is not None:
            sql += " AND batch_id=?"
            params.append(batch_id)
        with self._lock:
            rows = self.conn.execute(sql + " ORDER BY retry_at, message_id LIMIT 100", params)
            return [dict(row) for row in rows]

    def has_telegram_cleanup(self, batch_id: str) -> bool:
        with self._lock:
            return (
                self.conn.execute(
                    "SELECT 1 FROM telegram_cleanup WHERE batch_id=? LIMIT 1", (batch_id,)
                ).fetchone()
                is not None
            )

    def finish_telegram_cleanup(self, chat_id: str, message_ids: list[int]) -> None:
        with self._lock, self.conn:
            self.conn.executemany(
                "DELETE FROM telegram_cleanup WHERE chat_id=? AND message_id=?",
                [(chat_id, message_id) for message_id in message_ids],
            )

    def defer_telegram_cleanup(self, chat_id: str, failures: list[tuple[float, str, int]]) -> None:
        with self._lock, self.conn:
            self.conn.executemany(
                """UPDATE telegram_cleanup SET attempts=attempts+1, retry_at=?, last_error=?
                   WHERE message_id=? AND chat_id=?""",
                [(*failure, chat_id) for failure in failures],
            )
