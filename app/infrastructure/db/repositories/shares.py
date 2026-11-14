from __future__ import annotations

from typing import Any

from app.infrastructure.db.common import row_dict, utcnow
from app.infrastructure.db.session import SQLiteSession


class SharesRepository(SQLiteSession):
    def create_share(self, item_id: str, token: str, expires_at: str | None) -> dict[str, Any]:
        with self._lock, self.conn:
            cursor = self.conn.execute(
                """INSERT INTO shares (token, item_id, created_at, expires_at)
                   SELECT ?, id, ?, ? FROM items WHERE id=? AND kind='file' AND trash_root IS NULL""",
                (token, utcnow(), expires_at, item_id),
            )
            if not cursor.rowcount:
                raise LookupError("文件不存在")
        return {"token": token, "item_id": item_id, "expires_at": expires_at}

    def get_share(self, token: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT s.*, i.name, i.kind, i.size, i.mime, i.parent_id "
                "FROM shares s JOIN items i ON i.id = s.item_id WHERE s.token = ? AND i.trash_root IS NULL",
                (token,),
            ).fetchone()
        return row_dict(row)

    def list_shares(self, limit: int, offset: int) -> tuple[list[dict], int]:
        with self._lock:
            total = self.conn.execute("SELECT COUNT(*) FROM shares").fetchone()[0]
            rows = self.conn.execute(
                """SELECT s.*, i.name, i.trash_root IS NOT NULL AS suspended
                   FROM shares s JOIN items i ON i.id=s.item_id
                   ORDER BY s.created_at DESC, s.token LIMIT ? OFFSET ?""",
                (limit, offset),
            )
            return [dict(row) for row in rows], total

    def update_share(self, token: str, expires_at: str | None) -> bool:
        with self._lock, self.conn:
            return bool(
                self.conn.execute(
                    "UPDATE shares SET expires_at=? WHERE token=?", (expires_at, token)
                ).rowcount
            )

    def delete_share(self, token: str) -> bool:
        with self._lock, self.conn:
            return bool(self.conn.execute("DELETE FROM shares WHERE token = ?", (token,)).rowcount)
