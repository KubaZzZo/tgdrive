from __future__ import annotations

import sqlite3
import threading
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.categories import CATEGORY_LABELS, classify_file, effective_mime

SCHEMA_VERSION = 8


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


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

CREATE INDEX IF NOT EXISTS idx_uploads_state ON uploads(state);

CREATE TABLE IF NOT EXISTS shares (
    token TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_shares_item ON shares(item_id);
"""


class Database:
    """SQLite metadata store.

    A single connection is shared by the FastAPI event loop and the upload
    worker thread. sqlite3 is opened with ``check_same_thread=False`` and all
    public operations are serialized with a re-entrant lock.
    """

    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            current = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
            if current > SCHEMA_VERSION:
                raise RuntimeError(
                    f"数据库版本 {current} 高于程序支持版本 {SCHEMA_VERSION}，请升级程序"
                )
            if current < 1:
                self.conn.executescript(SCHEMA)
                self.conn.execute("PRAGMA user_version=1")
                self.conn.commit()

            if current < 2:
                with self.conn:
                    # Include DDL in the transaction so an interrupted backfill is retryable.
                    self.conn.execute("BEGIN IMMEDIATE")
                    self.conn.execute("ALTER TABLE items ADD COLUMN category TEXT")
                    rows = self.conn.execute(
                        "SELECT id, name, mime FROM items WHERE kind='file'"
                    ).fetchall()
                    self.conn.executemany(
                        "UPDATE items SET category=?, mime=? WHERE id=?",
                        [
                            (
                                classify_file(row["name"], row["mime"]),
                                effective_mime(row["name"], row["mime"]),
                                row["id"],
                            )
                            for row in rows
                        ],
                    )
                    self.conn.execute(
                        "CREATE INDEX idx_items_category ON items(category, created_at DESC)"
                    )
                    self.conn.execute("CREATE INDEX idx_items_created ON items(created_at DESC)")
                    self.conn.execute("PRAGMA user_version=2")

            if current < 3:
                with self.conn:
                    self.conn.execute("BEGIN IMMEDIATE")
                    self.conn.execute(
                        "ALTER TABLE items ADD COLUMN source TEXT NOT NULL DEFAULT 'web'"
                    )
                    self.conn.execute("CREATE INDEX idx_items_source ON items(source, tg_chat_id)")
                    self.conn.execute("""CREATE TABLE channel_messages (
                        chat_id TEXT NOT NULL, message_id INTEGER NOT NULL,
                        item_id TEXT REFERENCES items(id) ON DELETE SET NULL,
                        state TEXT NOT NULL, media_key TEXT, scan_run TEXT,
                        PRIMARY KEY(chat_id, message_id))""")
                    self.conn.execute("""CREATE TABLE channel_sync_state (
                        chat_id TEXT PRIMARY KEY, last_message_id INTEGER NOT NULL DEFAULT 0)""")
                    self.conn.execute("PRAGMA user_version=3")

            if current < 4:
                with self.conn:
                    self.conn.execute("BEGIN IMMEDIATE")
                    self.conn.execute(
                        "ALTER TABLE channel_messages ADD COLUMN remote_version TEXT NOT NULL DEFAULT ''"
                    )
                    self.conn.execute("PRAGMA user_version=4")

            if current < 5:
                with self.conn:
                    self.conn.execute("BEGIN IMMEDIATE")
                    self.conn.execute("""CREATE TABLE traffic_daily (
                        day TEXT NOT NULL, source TEXT NOT NULL,
                        inbound INTEGER NOT NULL DEFAULT 0,
                        outbound INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY(day, source))""")
                    self.conn.execute("""CREATE TABLE traffic_config (
                        id INTEGER PRIMARY KEY CHECK(id=1),
                        started_at TEXT NOT NULL,
                        quota_bytes INTEGER NOT NULL DEFAULT 100000000000)""")
                    self.conn.execute(
                        "INSERT INTO traffic_config(id,started_at) VALUES (1,?)", (utcnow(),)
                    )
                    self.conn.execute("PRAGMA user_version=5")

            if current < 6:
                with self.conn:
                    self.conn.execute("BEGIN IMMEDIATE")
                    self.conn.execute("""CREATE TABLE telegram_profiles (
                        id TEXT PRIMARY KEY, session_key TEXT NOT NULL,
                        chat_id TEXT NOT NULL, user_id INTEGER NOT NULL,
                        username TEXT, first_name TEXT, title TEXT NOT NULL)""")
                    self.conn.execute("""CREATE TABLE telegram_workspaces (
                        chat_id TEXT PRIMARY KEY, database_key TEXT NOT NULL)""")
                    self.conn.execute("""CREATE TABLE telegram_active (
                        id INTEGER PRIMARY KEY CHECK(id=1), profile_id TEXT NOT NULL)""")
                    self.conn.execute("PRAGMA user_version=6")

            if current < 7:
                with self.conn:
                    self.conn.execute("BEGIN IMMEDIATE")
                    # NULL = legacy row to adopt conservatively; empty string =
                    # explicitly renamed locally; otherwise last automatic name.
                    self.conn.execute("ALTER TABLE channel_messages ADD COLUMN remote_name TEXT")
                    self.conn.execute("PRAGMA user_version=7")

            if current < 8:
                with self.conn:
                    self.conn.execute("""CREATE TABLE IF NOT EXISTS cdn_usage (
                        uuid TEXT NOT NULL, period TEXT NOT NULL,
                        charged INTEGER NOT NULL, PRIMARY KEY(uuid, period))""")
                    self.conn.execute("PRAGMA user_version=8")

    def cdn_observe(self, rows: list[tuple[str, str, int]]) -> None:
        with self._lock, self.conn:
            self.conn.executemany(
                """INSERT INTO cdn_usage VALUES (?,?,?) ON CONFLICT(uuid,period)
                DO UPDATE SET charged=MAX(charged,excluded.charged)""",
                rows,
            )

    def cdn_totals(self) -> dict[tuple[str, str], int]:
        with self._lock:
            return {
                (r["uuid"], r["period"]): r["charged"]
                for r in self.conn.execute("SELECT * FROM cdn_usage")
            }

    def cdn_charge(self, limits: list[tuple[str, str, int]], size: int) -> bool:
        # One transaction protects concurrent streams and separate application workers.
        with self._lock, self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            for uuid, period, ceiling in limits:
                row = self.conn.execute(
                    "SELECT charged FROM cdn_usage WHERE uuid=? AND period=?",
                    (uuid, period),
                ).fetchone()
                if row is None or row[0] + size > ceiling:
                    return False
            for uuid, period, _ in limits:
                self.conn.execute(
                    "UPDATE cdn_usage SET charged=charged+? WHERE uuid=? AND period=?",
                    (size, uuid, period),
                )
            return True

    def telegram_profiles(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(row) for row in self.conn.execute("SELECT * FROM telegram_profiles").fetchall()
            ]

    def active_telegram_profile(self) -> dict[str, Any] | None:
        with self._lock:
            return self._dict(
                self.conn.execute(
                    "SELECT p.* FROM telegram_profiles p JOIN telegram_active a ON a.profile_id=p.id WHERE a.id=1"
                ).fetchone()
            )

    def telegram_workspace(self, chat_id: str) -> str | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT database_key FROM telegram_workspaces WHERE chat_id=?", (chat_id,)
            ).fetchone()
            return row[0] if row else None

    def save_telegram_profile(
        self, profile: dict[str, Any], database_key: str, activate: bool = True
    ) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO telegram_profiles VALUES (?,?,?,?,?,?,?)",
                tuple(
                    profile[key]
                    for key in (
                        "id",
                        "session_key",
                        "chat_id",
                        "user_id",
                        "username",
                        "first_name",
                        "title",
                    )
                ),
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO telegram_workspaces VALUES (?,?)",
                (profile["chat_id"], database_key),
            )
            if activate:
                self.conn.execute(
                    "INSERT OR REPLACE INTO telegram_active VALUES (1,?)", (profile["id"],)
                )

    def record_traffic(self, rows: list[tuple[str, str, int, int]]) -> None:
        with self._lock, self.conn:
            self.conn.executemany(
                """INSERT INTO traffic_daily(day,source,inbound,outbound) VALUES (?,?,?,?)
                ON CONFLICT(day,source) DO UPDATE SET
                inbound=inbound+excluded.inbound, outbound=outbound+excluded.outbound""",
                rows,
            )

    def traffic_config(self) -> dict[str, Any]:
        with self._lock:
            return dict(self.conn.execute("SELECT * FROM traffic_config WHERE id=1").fetchone())

    def update_traffic_config(self, quota_bytes: int) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE traffic_config SET quota_bytes=? WHERE id=1",
                (quota_bytes,),
            )

    def traffic_rows(self, since: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(row)
                for row in self.conn.execute(
                    "SELECT * FROM traffic_daily WHERE day>=? ORDER BY day", (since,)
                ).fetchall()
            ]

    def schema_version(self) -> int:
        with self._lock:
            return int(self.conn.execute("PRAGMA user_version").fetchone()[0])

    def ping(self) -> bool:
        try:
            with self._lock:
                self.conn.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

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
                    "SELECT * FROM items WHERE parent_id = ? ORDER BY kind ASC, lower(name) ASC",
                    (parent_id,),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM items WHERE parent_id IS NULL ORDER BY kind ASC, lower(name) ASC"
                ).fetchall()
        return [dict(r) for r in rows]

    def query_items(
        self,
        parent_id: str | None = None,
        *,
        scope: str = "folder",
        category: str | None = None,
        query: str = "",
        sort: str = "name",
        direction: str = "asc",
        limit: int = 60,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        conditions: list[str] = []
        params: list[Any] = []
        if category:
            conditions.extend(["kind='file'", "category=?"])
            params.append(category)
        elif scope == "recent":
            conditions.append("kind='file'")
        if scope == "folder":
            conditions.append("parent_id = ?" if parent_id else "parent_id IS NULL")
            if parent_id:
                params.append(parent_id)
        if query:
            # instr treats %, _ and backslashes literally rather than as LIKE wildcards.
            conditions.append("instr(lower(name), lower(?)) > 0")
            params.append(query)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        column = {
            "name": "name COLLATE NOCASE",
            "size": "size",
            "created_at": "created_at",
            "updated_at": "updated_at",
        }[sort]
        order = "DESC" if direction == "desc" else "ASC"
        with self._lock:
            total = self.conn.execute("SELECT COUNT(*) FROM items" + where, params).fetchone()[0]
            rows = self.conn.execute(
                "SELECT * FROM items"
                + where
                + f" ORDER BY (kind='folder') DESC, {column} {order}, id ASC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return [dict(row) for row in rows], int(total)

    def category_stats(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT category, COUNT(*) AS count, COALESCE(SUM(size), 0) AS bytes FROM items WHERE kind='file' GROUP BY category"
            ).fetchall()
        counts = {row["category"]: dict(row) for row in rows}
        return [
            {
                "id": key,
                "label": label,
                "count": counts.get(key, {}).get("count", 0),
                "bytes": counts.get(key, {}).get("bytes", 0),
            }
            for key, label in CATEGORY_LABELS.items()
        ]

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
        mime = effective_mime(name, mime)
        now = utcnow()
        item_id = uuid.uuid4().hex
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO items "
                "(id, parent_id, kind, name, size, mime, sha256, tg_chat_id, created_at, updated_at, category) "
                "VALUES (?, ?, 'file', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item_id,
                    parent_id,
                    name,
                    int(size),
                    mime,
                    sha256,
                    str(chat_id),
                    now,
                    now,
                    classify_file(name, mime),
                ),
            )
            for part in parts:
                self.conn.execute(
                    "INSERT INTO file_parts (item_id, part_index, tg_message_id, size) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        item_id,
                        int(part["part_index"]),
                        int(part["tg_message_id"]),
                        int(part["size"]),
                    ),
                )
        return self.get_item(item_id)  # type: ignore[return-value]

    def rename_item(self, item_id: str, name: str) -> dict[str, Any] | None:
        name = self._clean_name(name)
        with self._lock, self.conn:
            item = self.get_item(item_id)
            if not item:
                return None
            category = classify_file(name, item["mime"]) if item["kind"] == "file" else None
            self.conn.execute(
                "UPDATE items SET name = ?, category = ?, updated_at = ? WHERE id = ?",
                (name, category, utcnow(), item_id),
            )
            self.conn.execute(
                "UPDATE channel_messages SET remote_name='' WHERE item_id=?", (item_id,)
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
        self,
        parent_id: str | None,
        name: str,
        size: int,
        mime: str | None,
        temp_path: str,
        chunk_size: int,
    ) -> dict[str, Any]:
        name = self._clean_name(name)
        if int(size) <= 0:
            raise ValueError("文件大小必须大于 0")
        if int(chunk_size) <= 0:
            raise ValueError("分片大小必须大于 0")
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

    def list_uploads(
        self, states: tuple[str, ...] = ("uploading", "error")
    ) -> list[dict[str, Any]]:
        """未完成的上传任务，供前端刷新后断点续传。"""
        if not states:
            return []
        placeholders = ",".join("?" for _ in states)
        with self._lock:
            rows = self.conn.execute(
                f"SELECT * FROM uploads WHERE state IN ({placeholders}) ORDER BY created_at DESC",
                tuple(states),
            ).fetchall()
        return [dict(r) for r in rows]

    def recover_interrupted_uploads(self) -> int:
        """Mark tasks interrupted by a process restart as retryable errors."""
        with self._lock, self.conn:
            cursor = self.conn.execute(
                "UPDATE uploads SET state='error', error=?, updated_at=? WHERE state='processing'",
                ("服务重启导致 Telegram 写入中断，请检查文件后重试", utcnow()),
            )
        return int(cursor.rowcount)

    def list_stale_uploads(self, before: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM uploads WHERE state IN ('uploading','error') AND updated_at < ?",
                (before,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---------- channel synchronization ----------

    def channel_cursor(self, chat_id: str) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT last_message_id FROM channel_sync_state WHERE chat_id=?", (chat_id,)
            ).fetchone()
        return int(row[0]) if row else 0

    def channel_high_water(self, chat_id: str) -> int:
        with self._lock:
            return int(
                self.conn.execute(
                    "SELECT COALESCE(MAX(message_id),0) FROM channel_messages WHERE chat_id=?",
                    (chat_id,),
                ).fetchone()[0]
            )

    def advance_channel_cursor(self, chat_id: str, message_id: int) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                """INSERT INTO channel_sync_state VALUES (?,?)
                ON CONFLICT(chat_id) DO UPDATE SET last_message_id=MAX(last_message_id,excluded.last_message_id)""",
                (chat_id, message_id),
            )

    def import_channel_file(
        self, chat_id: str, message_id: int, file: dict[str, Any], scan_run: str | None = None
    ) -> bool:
        """Atomically import/update one remote document; tombstones prevent resurrection."""
        with self._lock, self.conn:
            seen = self.conn.execute(
                "SELECT * FROM channel_messages WHERE chat_id=? AND message_id=?",
                (chat_id, message_id),
            ).fetchone()
            if seen and seen["state"] != "imported":
                return False
            if seen:
                self.conn.execute(
                    "UPDATE channel_messages SET scan_run=COALESCE(?,scan_run) WHERE chat_id=? AND message_id=?",
                    (scan_run, chat_id, message_id),
                )
                if seen["remote_version"] > file["remote_version"]:
                    return False
                self.conn.execute(
                    "UPDATE channel_messages SET remote_version=? WHERE chat_id=? AND message_id=?",
                    (file["remote_version"], chat_id, message_id),
                )
                if seen["item_id"] and seen["media_key"] == file["media_key"]:
                    item = self.get_item(seen["item_id"])
                    previous = seen["remote_name"]
                    if previous is None:
                        candidates = {
                            file.get("original_name", file["name"]),
                            file.get("legacy_name", file["name"]),
                        }
                        previous = item["name"] if item and item["name"] in candidates else ""
                    changed = False
                    if item and previous and item["name"] == previous:
                        if item["name"] != file["name"]:
                            self.conn.execute(
                                "UPDATE items SET name=?,updated_at=? WHERE id=? AND source='channel'",
                                (file["name"], utcnow(), item["id"]),
                            )
                            changed = True
                        previous = file["name"]
                    self.conn.execute(
                        "UPDATE channel_messages SET remote_name=? WHERE chat_id=? AND message_id=?",
                        (previous, chat_id, message_id),
                    )
                    return changed
                if not seen["item_id"]:
                    return False
                # A Telegram media replacement updates the file without resetting local renames for caption-only edits.
                self.conn.execute(
                    "UPDATE items SET name=?,size=?,mime=?,category=?,updated_at=? WHERE id=? AND source='channel'",
                    (
                        file["name"],
                        file["size"],
                        file["mime"],
                        classify_file(file["name"], file["mime"]),
                        utcnow(),
                        seen["item_id"],
                    ),
                )
                self.conn.execute(
                    "UPDATE file_parts SET size=? WHERE item_id=?", (file["size"], seen["item_id"])
                )
                self.conn.execute(
                    "UPDATE channel_messages SET media_key=?,remote_name=? WHERE chat_id=? AND message_id=?",
                    (file["media_key"], file["name"], chat_id, message_id),
                )
                return True
            existing = self.conn.execute(
                """SELECT i.id FROM file_parts p JOIN items i ON i.id=p.item_id
                WHERE i.tg_chat_id=? AND p.tg_message_id=? LIMIT 1""",
                (chat_id, message_id),
            ).fetchone()
            if existing:
                self.conn.execute(
                    "INSERT INTO channel_messages(chat_id,message_id,item_id,state,media_key,scan_run) VALUES (?,?,?,'ignored',?,?)",
                    (chat_id, message_id, existing[0], file["media_key"], scan_run),
                )
                return False
            item_id = uuid.uuid4().hex
            now = utcnow()
            self.conn.execute(
                """INSERT INTO items (id,parent_id,kind,name,size,mime,tg_chat_id,created_at,updated_at,category,source)
                VALUES (?,NULL,'file',?,?,?,?,?,?,?,'channel')""",
                (
                    item_id,
                    file["name"],
                    file["size"],
                    file["mime"],
                    chat_id,
                    file["created_at"],
                    now,
                    classify_file(file["name"], file["mime"]),
                ),
            )
            self.conn.execute(
                "INSERT INTO file_parts VALUES (?,0,?,?)", (item_id, message_id, file["size"])
            )
            self.conn.execute(
                "INSERT INTO channel_messages(chat_id,message_id,item_id,state,media_key,scan_run,remote_version,remote_name) VALUES (?,?,?,'imported',?,?,?,?)",
                (
                    chat_id,
                    message_id,
                    item_id,
                    file["media_key"],
                    scan_run,
                    file["remote_version"],
                    file["name"],
                ),
            )
            return True

    def remove_channel_messages(self, chat_id: str, message_ids: list[int]) -> int:
        removed = 0
        with self._lock, self.conn:
            for mid in message_ids:
                row = self.conn.execute(
                    "SELECT item_id FROM channel_messages WHERE chat_id=? AND message_id=? AND state='imported'",
                    (chat_id, mid),
                ).fetchone()
                if row and row[0]:
                    removed += self.conn.execute(
                        "DELETE FROM items WHERE id=? AND source='channel'", (row[0],)
                    ).rowcount
                self.conn.execute(
                    """INSERT INTO channel_messages(chat_id,message_id,state) VALUES (?,?,'deleted')
                    ON CONFLICT(chat_id,message_id) DO UPDATE SET state='deleted',item_id=NULL""",
                    (chat_id, mid),
                )
        return removed

    def reconcile_channel(self, chat_id: str, scan_run: str, max_id: int) -> int:
        """Only prune after a complete, successful scan of a fixed history snapshot."""
        with self._lock:
            rows = self.conn.execute(
                """SELECT message_id FROM channel_messages WHERE chat_id=?
                AND state='imported' AND message_id<=? AND scan_run IS NOT ?""",
                (chat_id, max_id, scan_run),
            ).fetchall()
            return self.remove_channel_messages(chat_id, [int(row[0]) for row in rows])

    def channel_file_count(self, chat_id: str) -> int:
        with self._lock:
            return int(
                self.conn.execute(
                    "SELECT COUNT(*) FROM items WHERE source='channel' AND tg_chat_id=?", (chat_id,)
                ).fetchone()[0]
            )

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
