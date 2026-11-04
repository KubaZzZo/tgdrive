from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    parent_id TEXT REFERENCES items(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('file', 'folder')),
    name TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    mime TEXT,
    sha256 TEXT,
    tg_chat_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_items_parent ON items(parent_id);
CREATE INDEX IF NOT EXISTS idx_items_kind ON items(kind);

CREATE TABLE IF NOT EXISTS file_parts (
    item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    part_index INTEGER NOT NULL,
    tg_message_id INTEGER NOT NULL,
    size INTEGER NOT NULL,
    PRIMARY KEY (item_id, part_index)
);

CREATE TABLE IF NOT EXISTS uploads (
    id TEXT PRIMARY KEY,
    parent_id TEXT,
    name TEXT NOT NULL,
    size INTEGER NOT NULL,
    mime TEXT,
    temp_path TEXT NOT NULL,
    received INTEGER NOT NULL DEFAULT 0,
    chunk_size INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'uploading',
    progress INTEGER NOT NULL DEFAULT 0,
    item_id TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shares (
    token TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT
);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    # ---------- items ----------

    def list_items(self, parent_id: str | None) -> list[dict[str, Any]]:
        with self._lock:
            if parent_id:
                rows = self.conn.execute(
                    "SELECT * FROM items WHERE parent_id = ? "
                    "ORDER BY kind ASC, lower(name) ASC",
                    (parent_id,),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM items WHERE parent_id IS NULL "
                    "ORDER BY kind ASC, lower(name) ASC"
                ).fetchall()
        return [dict(r) for r in rows]

    def get_item(self, item_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return self._dict(row)

    def get_parts(self, item_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM file_parts WHERE item_id = ? ORDER BY part_index ASC",
                (item_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_path(self, item_id: str | None) -> list[dict[str, Any]]:
        path: list[dict[str, Any]] = []
        current = item_id
        seen: set[str] = set()
        while current:
            if current in seen:
                break
            seen.add(current)
            item = self.get_item(current)
            if not item:
                break
            path.append(item)
            current = item.get("parent_id")
        path.reverse()
        return path

    def create_folder(self, name: str, parent_id: str | None) -> dict[str, Any]:
        name = self._clean_name(name)
        now = utcnow()
        item_id = uuid.uuid4().hex
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO items (id, parent_id, kind, name, size, mime, created_at, updated_at) "
                "VALUES (?, ?, 'folder', ?, 0, NULL, ?, ?)",
                (item_id, parent_id, name, now, now),
            )
        return self.get_item(item_id)  # type: ignore[return-value]

    def create_file(
        self,
        name: str,
        parent_id: str | None,
        size: int,
        mime: str | None,
        sha256: str | None,
        chat_id: str,
        parts: Iterable[dict[str, Any]],
    ) -> dict[str, Any]:
        name = self._clean_name(name)
        now = utcnow()
        item_id = uuid.uuid4().hex
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO items "
                "(id, parent_id, kind, name, size, mime, sha256, tg_chat_id, created_at, updated_at) "
                "VALUES (?, ?, 'file', ?, ?, ?, ?, ?, ?, ?)",
                (item_id, parent_id, name, int(size), mime, sha256, str(chat_id), now, now),
            )
            for part in parts:
                self.conn.execute(
                    "INSERT INTO file_parts (item_id, part_index, tg_message_id, size) "
                    "VALUES (?, ?, ?, ?)",
                    (item_id, int(part["part_index"]), int(part["tg_message_id"]), int(part["size"])),
                )
        return self.get_item(item_id)  # type: ignore[return-value]

    def rename_item(self, item_id: str, name: str) -> dict[str, Any] | None:
        name = self._clean_name(name)
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE items SET name = ?, updated_at = ? WHERE id = ?",
                (name, utcnow(), item_id),
            )
        return self.get_item(item_id)

    def move_item(self, item_id: str, parent_id: str | None) -> dict[str, Any] | None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE items SET parent_id = ?, updated_at = ? WHERE id = ?",
                (parent_id, utcnow(), item_id),
            )
        return self.get_item(item_id)

    def is_descendant(self, item_id: str, possible_ancestor: str) -> bool:
        current: str | None = item_id
        seen: set[str] = set()
        while current and current not in seen:
            if current == possible_ancestor:
                return True
            seen.add(current)
            item = self.get_item(current)
            current = item.get("parent_id") if item else None
        return False

    def delete_tree(self, item_id: str) -> dict[str, Any]:
        """Delete an item and all children. Returns Telegram message ids to delete."""
        with self._lock:
            item = self.get_item(item_id)
            if not item:
                return {"deleted": 0, "message_ids": []}

            ids: list[str] = []
            queue = [item_id]
            while queue:
                current = queue.pop()
                ids.append(current)
                rows = self.conn.execute(
                    "SELECT id FROM items WHERE parent_id = ?", (current,)
                ).fetchall()
                queue.extend(r["id"] for r in rows)

            placeholders = ",".join("?" for _ in ids)
            message_rows = self.conn.execute(
                f"SELECT DISTINCT tg_message_id FROM file_parts WHERE item_id IN ({placeholders})",
                ids,
            ).fetchall()
            message_ids = [int(r["tg_message_id"]) for r in message_rows]

            with self.conn:
                self.conn.execute(f"DELETE FROM items WHERE id IN ({placeholders})", ids)
            return {"deleted": len(ids), "message_ids": message_ids}

    @staticmethod
    def _clean_name(name: str) -> str:
        name = (name or "").strip()
        if not name:
            raise ValueError("名称不能为空")
        if len(name) > 255:
            raise ValueError("名称过长")
        if "/" in name or "\\" in name or name in {".", ".."}:
            raise ValueError("名称包含非法字符")
        return name

    # ---------- uploads ----------

    def create_upload(
        self, parent_id: str | None, name: str, size: int, mime: str | None, temp_path: str, chunk_size: int
    ) -> dict[str, Any]:
        name = self._clean_name(name)
        upload_id = uuid.uuid4().hex
        now = utcnow()
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO uploads "
                "(id, parent_id, name, size, mime, temp_path, received, chunk_size, state, progress, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, ?, 'uploading', 0, ?, ?)",
                (upload_id, parent_id, name, int(size), mime, temp_path, int(chunk_size), now, now),
            )
        return self.get_upload(upload_id)  # type: ignore[return-value]

    def get_upload(self, upload_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM uploads WHERE id = ?", (upload_id,)).fetchone()
        return self._dict(row)

    def update_upload(self, upload_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {"received", "state", "progress", "item_id", "error"}
        values = {k: v for k, v in fields.items() if k in allowed}
        if not values:
            return self.get_upload(upload_id)
        values["updated_at"] = utcnow()
        sets = ", ".join(f"{k} = ?" for k in values)
        with self._lock, self.conn:
            self.conn.execute(
                f"UPDATE uploads SET {sets} WHERE id = ?",
                [*values.values(), upload_id],
            )
        return self.get_upload(upload_id)

    def delete_upload(self, upload_id: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM uploads WHERE id = ?", (upload_id,))

    def list_uploads(self, states: tuple[str, ...] = ("uploading", "error")) -> list[dict[str, Any]]:
        """未完成的上传任务，供前端刷新后断点续传。"""
        placeholders = ",".join("?" for _ in states)
        with self._lock:
            rows = self.conn.execute(
                f"SELECT * FROM uploads WHERE state IN ({placeholders}) ORDER BY created_at DESC",
                tuple(states),
            ).fetchall()
        return [self._dict(r) for r in rows]  # type: ignore[misc]

    # ---------- shares ----------

    def create_share(self, item_id: str, token: str, expires_at: str | None) -> dict[str, Any]:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO shares (token, item_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (token, item_id, utcnow(), expires_at),
            )
        return {"token": token, "item_id": item_id, "expires_at": expires_at}

    def get_share(self, token: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT s.*, i.name, i.kind, i.size, i.mime, i.parent_id "
                "FROM shares s JOIN items i ON i.id = s.item_id WHERE s.token = ?",
                (token,),
            ).fetchone()
        return self._dict(row)

    def delete_share(self, token: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM shares WHERE token = ?", (token,))

    # ---------- stats ----------

    def stats(self) -> dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT COALESCE(SUM(size), 0) AS bytes, COUNT(*) AS files "
                "FROM items WHERE kind = 'file'"
            ).fetchone()
            folders = self.conn.execute(
                "SELECT COUNT(*) AS n FROM items WHERE kind = 'folder'"
            ).fetchone()["n"]
        return {"bytes": int(row["bytes"]), "files": int(row["files"]), "folders": int(folders)}
