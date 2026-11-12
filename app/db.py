from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unicodedata import normalize

from app.categories import CATEGORY_LABELS, classify_file, effective_mime
from app.people import MAX_PERSON_LENGTH, person_key
from app.person_names import chinese_person_name

SCHEMA_VERSION = 16
PEOPLE_AI_MAX_ATTEMPTS = 3


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
        self.conn.create_function("person_display_name", 1, chinese_person_name, deterministic=True)
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

            if current < 10:
                # Versions 8/9 exist on deployed Bot-fallback instances. Some
                # schema-8 installs instead contain CDN counters. Reconcile all
                # three additive tables without dropping either installation's data.
                with self.conn:
                    self.conn.execute("BEGIN IMMEDIATE")
                    self.conn.execute("""CREATE TABLE IF NOT EXISTS channel_bot_files (
                        item_id TEXT PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
                        bot_id TEXT NOT NULL, file_id TEXT NOT NULL,
                        remote_version TEXT NOT NULL, size INTEGER NOT NULL)""")
                    self.conn.execute("""CREATE TABLE IF NOT EXISTS channel_bot_covers (
                        item_id TEXT PRIMARY KEY REFERENCES channel_bot_files(item_id) ON DELETE CASCADE,
                        file_id TEXT NOT NULL)""")
                    self.conn.execute("""CREATE TABLE IF NOT EXISTS cdn_usage (
                        uuid TEXT NOT NULL, period TEXT NOT NULL,
                        charged INTEGER NOT NULL, PRIMARY KEY(uuid, period))""")
                    self.conn.execute("PRAGMA user_version=10")

            if current < 11:
                with self._lock, self.conn:
                    self.conn.execute("BEGIN IMMEDIATE")
                    self.conn.execute(
                        "ALTER TABLE uploads ADD COLUMN image_host INTEGER NOT NULL DEFAULT 0"
                    )
                    self.conn.execute("""CREATE TABLE image_hosts (
                        item_id TEXT PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
                        token TEXT NOT NULL UNIQUE, mime TEXT NOT NULL, created_at TEXT NOT NULL)""")
                    self.conn.execute("PRAGMA user_version=11")

            if current < 12:
                with self.conn:
                    self.conn.execute("BEGIN IMMEDIATE")
                    self.conn.execute("""CREATE TABLE IF NOT EXISTS video_people (
                        item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                        person_key TEXT NOT NULL, name TEXT NOT NULL,
                        PRIMARY KEY(item_id, person_key))""")
                    self.conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_video_people_key ON video_people(person_key)"
                    )
                    self.conn.execute("PRAGMA user_version=12")

            if current < 13:
                with self.conn:
                    self.conn.execute("BEGIN IMMEDIATE")
                    self.conn.execute("""CREATE TABLE IF NOT EXISTS people_ai_jobs (
                        key TEXT PRIMARY KEY, caption TEXT, result TEXT,
                        retry_at REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0)""")
                    channel_columns = {
                        row["name"]
                        for row in self.conn.execute("PRAGMA table_info(channel_messages)")
                    }
                    if "people_ai_key" not in channel_columns:
                        self.conn.execute(
                            "ALTER TABLE channel_messages ADD COLUMN people_ai_key TEXT"
                        )
                    self.conn.execute("""CREATE INDEX IF NOT EXISTS idx_channel_messages_people_ai
                        ON channel_messages(people_ai_key) WHERE people_ai_key IS NOT NULL""")
                    people_columns = {
                        row["name"] for row in self.conn.execute("PRAGMA table_info(video_people)")
                    }
                    if "display_name" not in people_columns:
                        self.conn.execute("ALTER TABLE video_people ADD COLUMN display_name TEXT")
                        self.conn.execute(
                            "ALTER TABLE video_people ADD COLUMN aliases TEXT NOT NULL DEFAULT '[]'"
                        )
                    self.conn.execute("PRAGMA user_version=13")

            if current < 14:
                with self.conn:
                    self.conn.execute("BEGIN IMMEDIATE")
                    # A null name records a deleted label. Recreating it keeps old
                    # automatic associations hidden until explicitly assigned.
                    self.conn.execute("""CREATE TABLE IF NOT EXISTS people_labels (
                        person_key TEXT PRIMARY KEY, name TEXT,
                        include_automatic INTEGER NOT NULL DEFAULT 1)""")
                    self.conn.execute("""CREATE TABLE IF NOT EXISTS video_people_edits (
                        item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                        person_key TEXT NOT NULL, assigned INTEGER NOT NULL,
                        PRIMARY KEY(item_id, person_key))""")
                    self.conn.execute("""CREATE INDEX IF NOT EXISTS idx_video_people_edits_key
                        ON video_people_edits(person_key)""")
                    self.conn.execute("""CREATE VIEW IF NOT EXISTS effective_video_people AS
                        SELECT p.item_id, p.person_key, p.name, p.display_name, p.aliases
                        FROM video_people p
                        LEFT JOIN people_labels l ON l.person_key=p.person_key
                        LEFT JOIN video_people_edits e
                            ON e.item_id=p.item_id AND e.person_key=p.person_key
                        WHERE COALESCE(l.include_automatic, 1)=1
                            AND COALESCE(e.assigned, 1)=1
                        UNION ALL
                        SELECT e.item_id, l.person_key, l.name, NULL, '[]'
                        FROM video_people_edits e JOIN people_labels l ON l.person_key=e.person_key
                        WHERE e.assigned=1 AND l.name IS NOT NULL
                            AND (l.include_automatic=0 OR NOT EXISTS (
                                SELECT 1 FROM video_people p
                                WHERE p.item_id=e.item_id AND p.person_key=e.person_key))""")
                    self.conn.execute("PRAGMA user_version=14")

            if current < 15:
                with self.conn:
                    self.conn.execute("BEGIN IMMEDIATE")
                    self.conn.execute("""CREATE TABLE IF NOT EXISTS telegram_cleanup (
                        chat_id TEXT NOT NULL, message_id INTEGER NOT NULL,
                        batch_id TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                        retry_at REAL NOT NULL DEFAULT 0, last_error TEXT,
                        PRIMARY KEY (chat_id, message_id)
                    )""")
                    self.conn.execute("""CREATE INDEX IF NOT EXISTS idx_telegram_cleanup_due
                        ON telegram_cleanup(chat_id, retry_at)""")
                    self.conn.execute("""CREATE INDEX IF NOT EXISTS idx_telegram_cleanup_batch
                        ON telegram_cleanup(batch_id)""")
                    self.conn.execute("PRAGMA user_version=15")

            if current < 16:
                with self.conn:
                    self.conn.execute("BEGIN IMMEDIATE")
                    columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(items)")}
                    for column in ("trash_root", "trashed_at", "trash_parent_id"):
                        if column not in columns:
                            self.conn.execute(f"ALTER TABLE items ADD COLUMN {column} TEXT")
                    self.conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_items_trash_root ON items(trash_root)"
                    )
                    self.conn.execute("""CREATE INDEX IF NOT EXISTS idx_items_trash_expiry ON items(trashed_at)
                        WHERE trash_root=id""")
                    self.conn.execute("PRAGMA user_version=16")

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
            return self._dict(
                self.conn.execute("SELECT * FROM image_hosts WHERE token=?", (token,)).fetchone()
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

    def delete_telegram_profile(self, profile_id: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                """DELETE FROM telegram_profiles WHERE id=? AND NOT EXISTS (
                    SELECT 1 FROM telegram_active WHERE profile_id=telegram_profiles.id
                )""",
                (profile_id,),
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
                    """SELECT * FROM items WHERE parent_id = ? AND trash_root IS NULL
                       ORDER BY kind ASC, lower(name) ASC""",
                    (parent_id,),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    """SELECT * FROM items WHERE parent_id IS NULL AND trash_root IS NULL
                       ORDER BY kind ASC, lower(name) ASC"""
                ).fetchall()
        return [dict(r) for r in rows]

    def query_items(
        self,
        parent_id: str | None = None,
        *,
        scope: str = "folder",
        category: str | None = None,
        query: str = "",
        person: str | None = None,
        kind: str | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
        min_size: int | None = None,
        max_size: int | None = None,
        extension: str | None = None,
        sort: str = "name",
        direction: str = "asc",
        limit: int = 60,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        conditions = ["trash_root IS NULL"]
        params: list[Any] = []
        if kind:
            conditions.append("kind=?")
            params.append(kind)
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
        if any(value is not None for value in (min_size, max_size, extension)):
            conditions.append("kind='file'")
        for condition, value in (
            ("date(created_at)>=?", created_from),
            ("date(created_at)<=?", created_to),
            ("size>=?", min_size),
            ("size<=?", max_size),
        ):
            if value is not None:
                conditions.append(condition)
                params.append(value)
        if extension is not None:
            suffix = f".{extension}"
            conditions.append("substr(lower(name), -length(?))=lower(?)")
            params.extend((suffix, suffix))
        if person is not None:
            conditions.extend(
                [
                    "category='video'",
                    "id IN (SELECT item_id FROM effective_video_people WHERE person_key=?)",
                ]
            )
            params.append(person_key(person))
        where = " WHERE " + " AND ".join(conditions)
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

    def list_video_people(self, query: str, limit: int, offset: int) -> tuple[list[dict], int]:
        grouped = """SELECT p.person_key, MIN(p.name) AS name,
                            MIN(p.display_name) AS display_name, COUNT(p.item_id) AS count
                     FROM (
                         SELECT p.* FROM effective_video_people p JOIN items i ON i.id=p.item_id
                         WHERE i.category='video' AND i.trash_root IS NULL
                         UNION ALL
                         SELECT NULL, person_key, name, NULL, '[]' FROM people_labels
                         WHERE name IS NOT NULL
                     ) p GROUP BY p.person_key"""
        key = person_key(query)
        params: tuple[str, ...] = ()
        if key:
            # Match at the person level so an alias on one video finds all their videos.
            grouped += """ HAVING instr(p.person_key, ?) > 0
                OR instr(person_display_name(MIN(p.name)), ?) > 0
                OR MAX(instr(COALESCE(p.display_name, ''), ?)) > 0
                OR MAX(EXISTS(SELECT 1 FROM json_each(p.aliases) WHERE instr(value, ?) > 0))"""
            params = (key, key, key, key)
        with self._lock:
            total = self.conn.execute("SELECT COUNT(*) FROM (" + grouped + ")", params).fetchone()[
                0
            ]
            rows = self.conn.execute(
                grouped + " ORDER BY count DESC, p.person_key LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return [
            {
                "name": row["name"],
                "display_name": row["display_name"] or chinese_person_name(row["name"]),
                "count": row["count"],
            }
            for row in rows
        ], int(total)

    def video_person_display_name(self, name: str) -> str:
        with self._lock:
            label = self.conn.execute(
                """SELECT MIN(p.display_name) FROM effective_video_people p JOIN items i ON i.id=p.item_id
                   WHERE i.category='video' AND i.trash_root IS NULL AND p.person_key=?""",
                (person_key(name),),
            ).fetchone()[0]
        return label or chinese_person_name(name)

    def create_people_label(self, name: str) -> None:
        name = " ".join(normalize("NFKC", name).split())
        if not 0 < len(name) <= MAX_PERSON_LENGTH or not name.isprintable():
            raise ValueError("请输入 1 到 80 个可显示字符作为标签名称")
        with self._lock, self.conn:
            self.conn.execute(
                """INSERT INTO people_labels(person_key,name) VALUES (?,?)
                   ON CONFLICT(person_key) DO UPDATE SET name=COALESCE(name,excluded.name)""",
                (person_key(name), name),
            )

    def delete_people_label(self, name: str) -> None:
        key = person_key(name)
        if not key:
            raise ValueError("请输入标签名称")
        with self._lock, self.conn:
            self.conn.execute(
                """INSERT INTO people_labels VALUES (?,NULL,0)
                   ON CONFLICT(person_key) DO UPDATE SET name=NULL,include_automatic=0""",
                (key,),
            )
            self.conn.execute("DELETE FROM video_people_edits WHERE person_key=?", (key,))

    def _require_video(self, item_id: str) -> None:
        item = self.get_item(item_id)
        if not item:
            raise LookupError("视频不存在")
        if item["category"] != "video" or item["kind"] != "file":
            raise ValueError("只有视频可以分配人物标签")

    def video_people_labels(self, item_id: str) -> list[dict[str, str]]:
        with self._lock:
            self._require_video(item_id)
            rows = self.conn.execute(
                """SELECT person_key,name,display_name FROM effective_video_people
                   WHERE item_id=? ORDER BY person_key""",
                (item_id,),
            ).fetchall()
        return [
            {
                "key": row["person_key"],
                "name": row["name"],
                "display_name": row["display_name"] or chinese_person_name(row["name"]),
            }
            for row in rows
        ]

    def edit_video_people(self, item_id: str, add: list[str], remove: list[str]) -> None:
        additions = {person_key(name): name for name in add}
        removals = {person_key(name): name for name in remove}
        if "" in additions or "" in removals or additions.keys() & removals.keys():
            raise ValueError("标签名称无效或同时出现在添加和移除列表中")
        with self._lock, self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            self._require_video(item_id)
            for key, name in (additions | removals).items():
                label = self.conn.execute(
                    """SELECT name FROM people_labels WHERE person_key=? AND name IS NOT NULL
                       UNION ALL
                       SELECT p.name FROM effective_video_people p JOIN items i ON i.id=p.item_id
                       WHERE p.person_key=? AND i.category='video' AND i.trash_root IS NULL LIMIT 1""",
                    (key, key),
                ).fetchone()
                if label is None:
                    if key in additions:
                        raise ValueError(f"标签「{name}」已不存在，请重新选择")
                    continue
                self.conn.execute(
                    "INSERT OR IGNORE INTO people_labels(person_key,name) VALUES (?,?)",
                    (key, label["name"]),
                )
            self.conn.executemany(
                """INSERT INTO video_people_edits VALUES (?,?,?)
                   ON CONFLICT(item_id,person_key) DO UPDATE SET assigned=excluded.assigned""",
                [(item_id, key, 1) for key in additions] + [(item_id, key, 0) for key in removals],
            )

    def category_stats(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT category, COUNT(*) AS count, COALESCE(SUM(size), 0) AS bytes
                   FROM items WHERE kind='file' AND trash_root IS NULL GROUP BY category"""
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

    def get_item(self, item_id: str, *, include_trash: bool = False) -> dict[str, Any] | None:
        sql = "SELECT * FROM items WHERE id = ?"
        if not include_trash:
            sql += " AND trash_root IS NULL"
        with self._lock:
            row = self.conn.execute(sql, (item_id,)).fetchone()
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
        with self._lock, self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            self._require_folder(parent_id)
            return self._insert_folder(name, parent_id)

    def _require_folder(self, parent_id: str | None) -> None:
        if parent_id is None:
            return
        parent = self.get_item(parent_id)
        if not parent:
            raise LookupError("目标文件夹不存在")
        if parent["kind"] != "folder":
            raise ValueError("目标不是文件夹")

    def _insert_folder(self, name: str, parent_id: str | None) -> dict[str, Any]:
        now = utcnow()
        item_id = uuid.uuid4().hex
        self.conn.execute(
            "INSERT INTO items (id, parent_id, kind, name, size, mime, created_at, updated_at) "
            "VALUES (?, ?, 'folder', ?, 0, NULL, ?, ?)",
            (item_id, parent_id, name, now, now),
        )
        return self.get_item(item_id)  # type: ignore[return-value]

    def create_folder_paths(self, parent_id: str | None, paths: list[list[str]]) -> list[str]:
        cleaned = [
            tuple(self._clean_name(segment, preserve_spaces=True) for segment in path)
            for path in paths
        ]
        with self._lock, self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            self._require_folder(parent_id)
            resolved: dict[tuple[str, ...], str] = {}
            for path in cleaned:
                for depth in range(1, len(path) + 1):
                    prefix = path[:depth]
                    if prefix in resolved:
                        continue
                    parent = resolved[prefix[:-1]] if depth > 1 else parent_id
                    row = self.conn.execute(
                        """SELECT id FROM items WHERE parent_id IS ? AND name=?
                           AND kind='folder' AND trash_root IS NULL ORDER BY id LIMIT 1""",
                        (parent, prefix[-1]),
                    ).fetchone()
                    resolved[prefix] = (
                        row["id"] if row else self._insert_folder(prefix[-1], parent)["id"]
                    )
            return [resolved[path] for path in cleaned]

    def create_file(
        self,
        name: str,
        parent_id: str | None,
        size: int,
        mime: str | None,
        sha256: str | None,
        chat_id: str,
        parts: Iterable[dict[str, Any]],
        hosted_mime: str | None = None,
    ) -> dict[str, Any]:
        name = self._clean_name(name)
        mime = effective_mime(name, mime)
        now = utcnow()
        item_id = uuid.uuid4().hex
        with self._lock, self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            self._require_folder(parent_id)
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

            if hosted_mime:
                self.conn.execute(
                    "INSERT INTO image_hosts VALUES (?,?,?,?)",
                    (item_id, secrets.token_urlsafe(24), hosted_mime, now),
                )
        return self.get_item(item_id)  # type: ignore[return-value]

    def rename_item(self, item_id: str, name: str) -> dict[str, Any] | None:
        return self.update_item(item_id, name=name)

    def move_item(self, item_id: str, parent_id: str | None) -> dict[str, Any] | None:
        return self.update_item(item_id, move=True, parent_id=parent_id)

    def update_item(
        self,
        item_id: str,
        *,
        name: str | None = None,
        move: bool = False,
        parent_id: str | None = None,
    ) -> dict[str, Any] | None:
        with self._lock, self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            item = self.get_item(item_id)
            if not item or (name is None and not move):
                return item
            if move:
                self._require_folder(parent_id)
                if parent_id is not None and self.is_descendant(parent_id, item_id):
                    raise ValueError("不能移动到自身或其子目录")
            category = item["category"]
            if name is not None:
                name = self._clean_name(name)
                category = classify_file(name, item["mime"]) if item["kind"] == "file" else None
                self.conn.execute(
                    "UPDATE channel_messages SET remote_name='' WHERE item_id=?", (item_id,)
                )
            self.conn.execute(
                "UPDATE items SET parent_id=?, name=?, category=?, updated_at=? WHERE id=?",
                (
                    parent_id if move else item["parent_id"],
                    name if name is not None else item["name"],
                    category,
                    utcnow(),
                    item_id,
                ),
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

    @staticmethod
    def _clean_name(name: str, *, preserve_spaces: bool = False) -> str:
        trimmed = (name or "").strip()
        if not trimmed:
            raise ValueError("名称不能为空")
        name = name if preserve_spaces else trimmed
        if len(name) > 255:
            raise ValueError("名称过长")
        if "/" in name or "\\" in name or trimmed in {".", ".."}:
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
        image_host: bool = False,
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
            self.conn.execute(
                "UPDATE uploads SET image_host=? WHERE id=?", (int(image_host), upload_id)
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

    def _replace_video_people(
        self, item_id: str, names: Iterable[str], analysis: Iterable[dict] = ()
    ) -> bool:
        people = {person_key(name): (name, None, "[]") for name in names}
        for person in analysis:
            key = person_key(person["name"])
            existing = people.get(key)
            people[key] = (
                existing[0] if existing else person["name"],
                person["display_name"] or None,
                json.dumps(sorted({person_key(alias) for alias in person["aliases"]})),
            )
        previous = {
            row["person_key"]: (row["name"], row["display_name"], row["aliases"])
            for row in self.conn.execute(
                "SELECT person_key,name,display_name,aliases FROM video_people WHERE item_id=?",
                (item_id,),
            )
        }
        if previous == people:
            return False
        self.conn.execute("DELETE FROM video_people WHERE item_id=?", (item_id,))
        self.conn.executemany(
            """INSERT INTO video_people(item_id,person_key,name,display_name,aliases)
               VALUES (?,?,?,?,?)""",
            [(item_id, key, *data) for key, data in people.items()],
        )
        return True

    def _sync_video_people(self, chat_id: str, message_id: int, item_id: str, file: dict) -> bool:
        key = None
        analysis = ()
        if job := file.get("people_ai_job"):
            candidate, caption = job
            if file.get("people_ai_submit"):
                self.conn.execute(
                    "INSERT OR IGNORE INTO people_ai_jobs(key,caption) VALUES (?,?)",
                    (candidate, caption),
                )
            cached = self.conn.execute(
                "SELECT result FROM people_ai_jobs WHERE key=?", (candidate,)
            ).fetchone()
            if cached is not None:
                key = candidate
                if cached["result"] is not None:
                    analysis = json.loads(cached["result"])
        self.conn.execute(
            """UPDATE channel_messages SET people_ai_key=?
               WHERE chat_id=? AND message_id=? AND people_ai_key IS NOT ?""",
            (key, chat_id, message_id, key),
        )
        return self._replace_video_people(item_id, file.get("people", ()), analysis)

    def next_people_ai_job(self, namespace: str, now: float) -> tuple[str, str] | None:
        with self._lock:
            row = self.conn.execute(
                """SELECT j.key,j.caption FROM people_ai_jobs j
                   WHERE j.key LIKE ? AND j.caption IS NOT NULL AND j.retry_at<=? AND j.attempts<?
                     AND EXISTS (SELECT 1 FROM channel_messages c WHERE c.people_ai_key=j.key
                                 AND c.state='imported' AND c.item_id IS NOT NULL)
                   ORDER BY j.retry_at,j.rowid LIMIT 1""",
                (namespace + ":%", now, PEOPLE_AI_MAX_ATTEMPTS),
            ).fetchone()
        return (row["key"], row["caption"]) if row else None

    def defer_people_ai_job(self, key: str, retry_at: float) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE people_ai_jobs SET retry_at=?, attempts=attempts+1,
                   caption=CASE WHEN attempts+1>=? THEN NULL ELSE caption END
                   WHERE key=? AND result IS NULL""",
                (retry_at, PEOPLE_AI_MAX_ATTEMPTS, key),
            )

    def finish_people_ai_job(self, key: str, people: list[dict]) -> int:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE people_ai_jobs SET result=?,caption=NULL WHERE key=?",
                (json.dumps(people, ensure_ascii=False), key),
            )
            items = self.conn.execute(
                """SELECT c.item_id FROM channel_messages c JOIN items i ON i.id=c.item_id
                   WHERE c.people_ai_key=? AND c.state='imported' AND i.category='video'""",
                (key,),
            ).fetchall()
            changed = 0
            for item in items:
                item_id = item["item_id"]
                names = [
                    row["name"]
                    for row in self.conn.execute(
                        "SELECT name FROM video_people WHERE item_id=?", (item_id,)
                    )
                ]
                changed += self._replace_video_people(item_id, names, people)
            return changed

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
                    item = self.get_item(seen["item_id"], include_trash=True)
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
                    people_changed = self._sync_video_people(
                        chat_id, message_id, seen["item_id"], file
                    )
                    return changed or people_changed
                if not seen["item_id"]:
                    return False
                item = self.get_item(seen["item_id"], include_trash=True)
                same_post = (seen["media_key"] or "").startswith("bot:") and seen[
                    "remote_version"
                ] == file["remote_version"]
                keep_name = same_post and item and item["name"] != seen["remote_name"]
                name = item["name"] if keep_name else file["name"]
                # Publication consent belongs to the original media, not its message slot.
                # Caption-only updates return above and preserve the public link.
                if seen["media_key"] != file["media_key"]:
                    self.conn.execute("DELETE FROM image_hosts WHERE item_id=?", (seen["item_id"],))
                # A Telegram media replacement updates the file without resetting local renames for caption-only edits.
                self.conn.execute(
                    "UPDATE items SET name=?,size=?,mime=?,category=?,updated_at=? WHERE id=? AND source='channel'",
                    (
                        name,
                        file["size"],
                        file["mime"],
                        classify_file(name, file["mime"]),
                        utcnow(),
                        seen["item_id"],
                    ),
                )
                self.conn.execute(
                    "UPDATE file_parts SET size=? WHERE item_id=?", (file["size"], seen["item_id"])
                )
                self.conn.execute(
                    "UPDATE channel_messages SET media_key=?,remote_name=? WHERE chat_id=? AND message_id=?",
                    (file["media_key"], "" if keep_name else file["name"], chat_id, message_id),
                )
                self._sync_video_people(chat_id, message_id, seen["item_id"], file)
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
            self._sync_video_people(chat_id, message_id, item_id, file)
            return True

    def import_bot_file(
        self,
        chat_id: str,
        message_id: int,
        file: dict[str, Any],
        bot_id: str,
        file_id: str,
        cover_id: str | None = None,
    ) -> bool:
        with self._lock:
            sql = "SELECT item_id, state, remote_version, media_key FROM channel_messages WHERE chat_id=? AND message_id=?"
            params = (chat_id, message_id)
            seen = self.conn.execute(sql, params).fetchone()
            changed = False
            if not seen or (
                seen["state"] == "imported"
                and (
                    (
                        seen["media_key"] == file["media_key"]
                        and seen["remote_version"] <= file["remote_version"]
                    )
                    or (
                        (seen["media_key"] or "").startswith("bot:")
                        and seen["remote_version"] < file["remote_version"]
                    )
                )
            ):
                changed = self.import_channel_file(chat_id, message_id, file)
                seen = self.conn.execute(sql, params).fetchone()
            if (
                seen
                and seen["item_id"]
                and seen["state"] == "imported"
                and seen["remote_version"] == file["remote_version"]
            ):
                item_id = seen["item_id"]
                old_cover = self.conn.execute(
                    "SELECT file_id FROM channel_bot_covers WHERE item_id=?", (item_id,)
                ).fetchone()
                with self.conn:
                    self.conn.execute(
                        "INSERT OR REPLACE INTO channel_bot_files VALUES (?,?,?,?,?)",
                        (item_id, bot_id, file_id, file["remote_version"], file["size"]),
                    )
                    if cover_id:
                        self.conn.execute(
                            "INSERT INTO channel_bot_covers VALUES (?,?)", (item_id, cover_id)
                        )
                    if cover_id != (old_cover[0] if old_cover else None):
                        self.conn.execute(
                            "UPDATE items SET updated_at=? WHERE id=?", (utcnow(), item_id)
                        )
                        changed = True
            return changed

    def bot_file_id(self, item_id: str, bot_id: str, *, cover: bool = False) -> str | None:
        with self._lock:
            row = self.conn.execute(
                """SELECT b.file_id, c.file_id FROM channel_bot_files b JOIN channel_messages m USING (item_id)
                JOIN items i ON i.id=b.item_id
                LEFT JOIN channel_bot_covers c ON c.item_id=b.item_id
                WHERE b.item_id=? AND b.bot_id=? AND m.state='imported'
                AND b.remote_version=m.remote_version AND b.size=i.size""",
                (item_id, bot_id),
            ).fetchone()
            return row[1 if cover else 0] if row else None

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
                    """SELECT COUNT(*) FROM items WHERE source='channel' AND tg_chat_id=?
                   AND trash_root IS NULL""",
                    (chat_id,),
                ).fetchone()[0]
            )

    # ---------- shares ----------

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
        return self._dict(row)

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

    # ---------- stats ----------

    def stats(self) -> dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT COALESCE(SUM(size), 0) AS bytes, COUNT(*) AS files "
                "FROM items WHERE kind = 'file' AND trash_root IS NULL"
            ).fetchone()
            folders = self.conn.execute(
                "SELECT COUNT(*) AS n FROM items WHERE kind = 'folder' AND trash_root IS NULL"
            ).fetchone()["n"]
        return {"bytes": int(row["bytes"]), "files": int(row["files"]), "folders": int(folders)}
