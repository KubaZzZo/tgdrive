from __future__ import annotations

import uuid
from typing import Any

from app.infrastructure.db.common import utcnow
from app.infrastructure.db.repositories.files import FilesRepository


class TrashRepository(FilesRepository):
    def trash_tree(self, item_id: str) -> int:
        with self._lock, self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            if not self.get_item(item_id):
                return 0
            return self._trash_tree(item_id)

    def trash_items(self, item_ids: list[str]) -> set[str]:
        with self._lock, self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            paths = {item_id: self.get_path(item_id) for item_id in item_ids}
            active = {item_id for item_id, path in paths.items() if path}
            for item_id in active:
                if not any(parent["id"] in active for parent in paths[item_id][:-1]):
                    self._trash_tree(item_id)
            trashed = set(active)
            for item_id in paths.keys() - active:
                item = self.get_item(item_id, include_trash=True)
                if item and item["trash_root"] is not None:
                    trashed.add(item_id)
            return trashed

    def _trash_tree(self, item_id: str) -> int:
        self.conn.execute(
            """WITH RECURSIVE tree(id) AS (
                   SELECT ? UNION ALL SELECT i.id FROM items i JOIN tree ON i.parent_id=tree.id
                   WHERE i.trash_root IS NULL
               )
               UPDATE items SET trash_root=?, trashed_at=? WHERE id IN (SELECT id FROM tree)""",
            (item_id, item_id, utcnow()),
        )
        count = self.conn.execute("SELECT changes()").fetchone()[0]
        self.conn.execute(
            "UPDATE items SET trash_parent_id=parent_id, parent_id=NULL WHERE id=?", (item_id,)
        )
        return int(count)

    def list_trash(self, limit: int, offset: int) -> tuple[list[dict], int]:
        with self._lock:
            total = self.conn.execute("SELECT COUNT(*) FROM items WHERE trash_root=id").fetchone()[
                0
            ]
            rows = self.conn.execute(
                """SELECT * FROM items WHERE trash_root=id
                   ORDER BY trashed_at DESC, id LIMIT ? OFFSET ?""",
                (limit, offset),
            )
            return [dict(row) for row in rows], total

    def restore_trash(self, item_id: str) -> dict[str, Any] | None:
        with self._lock, self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            item = self.get_item(item_id, include_trash=True)
            if not item or item["trash_root"] != item_id:
                return None
            original_parent = item["trash_parent_id"]
            parent = self.get_item(original_parent) if original_parent else None
            parent_id = original_parent if parent and parent["kind"] == "folder" else None
            self.conn.execute(
                "UPDATE items SET parent_id=?, trash_parent_id=NULL WHERE id=?",
                (parent_id, item_id),
            )
            self.conn.execute(
                "UPDATE items SET trash_root=NULL, trashed_at=NULL WHERE trash_root=?", (item_id,)
            )
            return self.get_item(item_id)

    def trash_ids(self, before: str, limit: int) -> list[str]:
        with self._lock:
            return [
                row[0]
                for row in self.conn.execute(
                    """SELECT id FROM items WHERE trash_root=id AND trashed_at<=?
                       ORDER BY trashed_at, id LIMIT ?""",
                    (before, limit),
                )
            ]

    def delete_tree(
        self,
        item_id: str,
        *,
        fallback_chat_id: str = "",
        trashed_before: str | None = None,
    ) -> dict[str, Any]:
        """Delete the tree and persist its Telegram cleanup in the same transaction."""
        batch_id = uuid.uuid4().hex
        with self._lock, self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            item = self.get_item(item_id, include_trash=trashed_before is not None)
            if not item or (
                trashed_before is not None
                and (item["trash_root"] != item_id or item["trashed_at"] > trashed_before)
            ):
                return {"deleted": 0, "message_ids": [], "batch_id": batch_id}

            tree = """WITH RECURSIVE tree(id) AS (
                SELECT ? UNION ALL SELECT i.id FROM items i JOIN tree ON i.parent_id=tree.id
            ) """
            count = self.conn.execute(tree + "SELECT COUNT(*) FROM tree", (item_id,)).fetchone()[0]
            message_rows = self.conn.execute(
                tree
                + """SELECT DISTINCT items.tg_chat_id, file_parts.tg_message_id
                          FROM tree JOIN items USING (id) JOIN file_parts ON file_parts.item_id=tree.id""",
                (item_id,),
            ).fetchall()
            messages = {
                (row["tg_chat_id"] or fallback_chat_id, int(row["tg_message_id"]))
                for row in message_rows
            }
            self.conn.executemany(
                """INSERT INTO telegram_cleanup(chat_id, message_id, batch_id) VALUES (?,?,?)
                   ON CONFLICT(chat_id, message_id) DO UPDATE SET batch_id=excluded.batch_id""",
                [(chat_id, message_id, batch_id) for chat_id, message_id in messages],
            )
            self.conn.executemany(
                """INSERT INTO channel_messages(chat_id,message_id,state) VALUES (?,?,'deleted')
                   ON CONFLICT(chat_id,message_id) DO UPDATE SET state='deleted',item_id=NULL""",
                messages,
            )
            self.conn.execute("DELETE FROM items WHERE id=?", (item_id,))
            return {
                "deleted": count,
                "message_ids": sorted({message_id for _, message_id in messages}),
                "batch_id": batch_id,
            }
