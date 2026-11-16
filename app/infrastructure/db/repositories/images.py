from __future__ import annotations

from typing import Any

from app.infrastructure.db.common import row_dict, utcnow
from app.infrastructure.db.repositories.files import FilesRepository


class ImagesRepository(FilesRepository):
    def publish_image(self, item_id: str, token: str, mime: str) -> dict[str, Any]:
        with self._lock, self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            if not self.get_item(item_id):
                raise LookupError("图片不存在")
            self.conn.execute(
                "INSERT OR IGNORE INTO image_hosts VALUES (?,?,?,?)",
                (item_id, token, mime, utcnow()),
            )
            return dict(
                self.conn.execute(
                    "SELECT * FROM image_hosts WHERE item_id=?", (item_id,)
                ).fetchone()
            )

    def hosted_image(self, token: str) -> dict[str, Any] | None:
        with self._lock:
            return row_dict(
                self.conn.execute("SELECT * FROM image_hosts WHERE token=?", (token,)).fetchone()
            )

    def hosted_image_for_item(self, item_id: str) -> dict[str, Any] | None:
        with self._lock:
            return row_dict(
                self.conn.execute(
                    "SELECT * FROM image_hosts WHERE item_id=?", (item_id,)
                ).fetchone()
            )

    def list_hosted_images(self, query: str, limit: int, offset: int) -> tuple[list[dict], int]:
        with self._lock:
            where = """ FROM image_hosts h JOIN items i ON i.id=h.item_id
                        WHERE i.trash_root IS NULL AND instr(lower(i.name),lower(?))>0"""
            total = self.conn.execute("SELECT COUNT(*)" + where, (query,)).fetchone()[0]
            rows = self.conn.execute(
                "SELECT i.*,h.token,h.mime AS hosted_mime,h.created_at AS published_at"
                + where
                + " ORDER BY h.created_at DESC,h.item_id LIMIT ? OFFSET ?",
                (query, limit, offset),
            ).fetchall()
            return [dict(r) for r in rows], total

    def unpublish_image(self, item_id: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM image_hosts WHERE item_id=?", (item_id,))

    def invalidate_web_media_for_message(self, chat_id: str, message_id: int) -> int:
        items = """SELECT i.id FROM items i JOIN file_parts p ON p.item_id=i.id
                   WHERE i.source='web' AND i.tg_chat_id=? AND p.tg_message_id=?"""
        with self._lock, self.conn:
            revoked = self.conn.execute(
                f"DELETE FROM image_hosts WHERE item_id IN ({items})",
                (chat_id, message_id),
            ).rowcount
            changed = self.conn.execute(
                f"UPDATE items SET updated_at=? WHERE id IN ({items})",
                (utcnow(), chat_id, message_id),
            ).rowcount
            return revoked + changed
