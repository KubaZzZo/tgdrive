from __future__ import annotations

import sqlite3

from app.infrastructure.db.common import utcnow
from app.services.categories import classify_file, effective_mime

SCHEMA_VERSION = 21

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


def _upgrade_to_1(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA user_version=1")
    conn.commit()


def _upgrade_to_2(conn: sqlite3.Connection) -> None:
    with conn:
        # Include DDL in the transaction so an interrupted backfill is retryable.
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ALTER TABLE items ADD COLUMN category TEXT")
        rows = conn.execute("SELECT id, name, mime FROM items WHERE kind='file'").fetchall()
        conn.executemany(
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
        conn.execute("CREATE INDEX idx_items_category ON items(category, created_at DESC)")
        conn.execute("CREATE INDEX idx_items_created ON items(created_at DESC)")
        conn.execute("PRAGMA user_version=2")


def _upgrade_to_3(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ALTER TABLE items ADD COLUMN source TEXT NOT NULL DEFAULT 'web'")
        conn.execute("CREATE INDEX idx_items_source ON items(source, tg_chat_id)")
        conn.execute("""CREATE TABLE channel_messages (
            chat_id TEXT NOT NULL, message_id INTEGER NOT NULL,
            item_id TEXT REFERENCES items(id) ON DELETE SET NULL,
            state TEXT NOT NULL, media_key TEXT, scan_run TEXT,
            PRIMARY KEY(chat_id, message_id))""")
        conn.execute("""CREATE TABLE channel_sync_state (
            chat_id TEXT PRIMARY KEY, last_message_id INTEGER NOT NULL DEFAULT 0)""")
        conn.execute("PRAGMA user_version=3")


def _upgrade_to_4(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "ALTER TABLE channel_messages ADD COLUMN remote_version TEXT NOT NULL DEFAULT ''"
        )
        conn.execute("PRAGMA user_version=4")


def _upgrade_to_5(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""CREATE TABLE traffic_daily (
            day TEXT NOT NULL, source TEXT NOT NULL,
            inbound INTEGER NOT NULL DEFAULT 0,
            outbound INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(day, source))""")
        conn.execute("""CREATE TABLE traffic_config (
            id INTEGER PRIMARY KEY CHECK(id=1),
            started_at TEXT NOT NULL,
            quota_bytes INTEGER NOT NULL DEFAULT 100000000000)""")
        conn.execute("INSERT INTO traffic_config(id,started_at) VALUES (1,?)", (utcnow(),))
        conn.execute("PRAGMA user_version=5")


def _upgrade_to_6(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""CREATE TABLE telegram_profiles (
            id TEXT PRIMARY KEY, session_key TEXT NOT NULL,
            chat_id TEXT NOT NULL, user_id INTEGER NOT NULL,
            username TEXT, first_name TEXT, title TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE telegram_workspaces (
            chat_id TEXT PRIMARY KEY, database_key TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE telegram_active (
            id INTEGER PRIMARY KEY CHECK(id=1), profile_id TEXT NOT NULL)""")
        conn.execute("PRAGMA user_version=6")


def _upgrade_to_7(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        # NULL = legacy row to adopt conservatively; empty string =
        # explicitly renamed locally; otherwise last automatic name.
        conn.execute("ALTER TABLE channel_messages ADD COLUMN remote_name TEXT")
        conn.execute("PRAGMA user_version=7")


def _ensure_bot_files(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS channel_bot_files (
        item_id TEXT PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
        bot_id TEXT NOT NULL, file_id TEXT NOT NULL,
        remote_version TEXT NOT NULL, size INTEGER NOT NULL)""")


def _ensure_bot_covers(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS channel_bot_covers (
        item_id TEXT PRIMARY KEY REFERENCES channel_bot_files(item_id) ON DELETE CASCADE,
        file_id TEXT NOT NULL)""")


def _upgrade_to_8(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_bot_files(conn)
        conn.execute("PRAGMA user_version=8")


def _upgrade_to_9(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        # Schema 8 was also used for CDN-only installs, without Bot tables.
        _ensure_bot_files(conn)
        _ensure_bot_covers(conn)
        conn.execute("PRAGMA user_version=9")


def _upgrade_to_10(conn: sqlite3.Connection) -> None:
    # Versions 8/9 exist on deployed Bot-fallback instances. Some
    # schema-8 installs instead contain CDN counters. Reconcile all
    # three additive tables without dropping either installation's data.
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_bot_files(conn)
        _ensure_bot_covers(conn)
        conn.execute("""CREATE TABLE IF NOT EXISTS cdn_usage (
            uuid TEXT NOT NULL, period TEXT NOT NULL,
            charged INTEGER NOT NULL, PRIMARY KEY(uuid, period))""")
        # IF NOT EXISTS preserves legacy data, but cannot verify an existing table.
        # Reject incompatible layouts before advancing the migration version.
        for table, required in (
            ("channel_bot_files", {"item_id", "bot_id", "file_id", "remote_version", "size"}),
            ("channel_bot_covers", {"item_id", "file_id"}),
            ("cdn_usage", {"uuid", "period", "charged"}),
        ):
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if not required.issubset(columns):
                raise RuntimeError(f"数据库迁移 10 失败：{table} 缺少必要字段，请检查历史数据库")
        conn.execute("PRAGMA user_version=10")


def _upgrade_to_11(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ALTER TABLE uploads ADD COLUMN image_host INTEGER NOT NULL DEFAULT 0")
        conn.execute("""CREATE TABLE image_hosts (
            item_id TEXT PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
            token TEXT NOT NULL UNIQUE, mime TEXT NOT NULL, created_at TEXT NOT NULL)""")
        conn.execute("PRAGMA user_version=11")


def _upgrade_to_12(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""CREATE TABLE IF NOT EXISTS video_people (
            item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            person_key TEXT NOT NULL, name TEXT NOT NULL,
            PRIMARY KEY(item_id, person_key))""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_video_people_key ON video_people(person_key)")
        conn.execute("PRAGMA user_version=12")


def _upgrade_to_13(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""CREATE TABLE IF NOT EXISTS people_ai_jobs (
            key TEXT PRIMARY KEY, caption TEXT, result TEXT,
            retry_at REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0)""")
        channel_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(channel_messages)")
        }
        if "people_ai_key" not in channel_columns:
            conn.execute("ALTER TABLE channel_messages ADD COLUMN people_ai_key TEXT")
        conn.execute("""CREATE INDEX IF NOT EXISTS idx_channel_messages_people_ai
            ON channel_messages(people_ai_key) WHERE people_ai_key IS NOT NULL""")
        people_columns = {row["name"] for row in conn.execute("PRAGMA table_info(video_people)")}
        if "display_name" not in people_columns:
            conn.execute("ALTER TABLE video_people ADD COLUMN display_name TEXT")
            conn.execute("ALTER TABLE video_people ADD COLUMN aliases TEXT NOT NULL DEFAULT '[]'")
        conn.execute("PRAGMA user_version=13")


def _upgrade_to_14(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        # A null name records a deleted label. Recreating it keeps old
        # automatic associations hidden until explicitly assigned.
        conn.execute("""CREATE TABLE IF NOT EXISTS people_labels (
            person_key TEXT PRIMARY KEY, name TEXT,
            include_automatic INTEGER NOT NULL DEFAULT 1)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS video_people_edits (
            item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            person_key TEXT NOT NULL, assigned INTEGER NOT NULL,
            PRIMARY KEY(item_id, person_key))""")
        conn.execute("""CREATE INDEX IF NOT EXISTS idx_video_people_edits_key
            ON video_people_edits(person_key)""")
        conn.execute("""CREATE VIEW IF NOT EXISTS effective_video_people AS
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
        conn.execute("PRAGMA user_version=14")


def _upgrade_to_15(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""CREATE TABLE IF NOT EXISTS telegram_cleanup (
            chat_id TEXT NOT NULL, message_id INTEGER NOT NULL,
            batch_id TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            retry_at REAL NOT NULL DEFAULT 0, last_error TEXT,
            PRIMARY KEY (chat_id, message_id)
        )""")
        conn.execute("""CREATE INDEX IF NOT EXISTS idx_telegram_cleanup_due
            ON telegram_cleanup(chat_id, retry_at)""")
        conn.execute("""CREATE INDEX IF NOT EXISTS idx_telegram_cleanup_batch
            ON telegram_cleanup(batch_id)""")
        conn.execute("PRAGMA user_version=15")


def _upgrade_to_16(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
        for column in ("trash_root", "trashed_at", "trash_parent_id"):
            if column not in columns:
                conn.execute(f"ALTER TABLE items ADD COLUMN {column} TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_trash_root ON items(trash_root)")
        conn.execute("""CREATE INDEX IF NOT EXISTS idx_items_trash_expiry ON items(trashed_at)
            WHERE trash_root=id""")
        conn.execute("PRAGMA user_version=16")


def _upgrade_to_17(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(uploads)")}
        if "link_import" not in columns:
            conn.execute("ALTER TABLE uploads ADD COLUMN link_import INTEGER NOT NULL DEFAULT 0")
        conn.execute("""CREATE TABLE IF NOT EXISTS video_import_batch (
            id INTEGER PRIMARY KEY CHECK (id=1), data TEXT NOT NULL
        )""")
        conn.execute("PRAGMA user_version=17")


def _upgrade_to_18(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""CREATE TABLE IF NOT EXISTS client_credentials (
            id TEXT PRIMARY KEY,
            chat_id TEXT NOT NULL,
            label TEXT NOT NULL,
            scope TEXT NOT NULL CHECK(scope IN ('picgo','webdav')),
            digest TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        )""")
        conn.execute("PRAGMA user_version=18")


def _upgrade_to_19(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""CREATE TABLE IF NOT EXISTS login_failures (
            client_key TEXT NOT NULL, occurred_at REAL NOT NULL
        )""")
        conn.execute("""CREATE INDEX IF NOT EXISTS idx_login_failures_client
            ON login_failures(client_key, occurred_at)""")
        conn.execute("""CREATE INDEX IF NOT EXISTS idx_login_failures_expiry
            ON login_failures(occurred_at)""")
        conn.execute("PRAGMA user_version=19")


def _upgrade_to_20(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(cdn_usage)")}
        if "provider_used" not in columns:
            conn.execute(
                "ALTER TABLE cdn_usage ADD COLUMN provider_used INTEGER NOT NULL DEFAULT 0"
            )
        if "provider_at" not in columns:
            conn.execute("ALTER TABLE cdn_usage ADD COLUMN provider_at REAL")
        if "legacy_at" not in columns:
            conn.execute("ALTER TABLE cdn_usage ADD COLUMN legacy_at REAL")
            conn.execute(
                "UPDATE cdn_usage SET legacy_at = CAST(strftime('%s', 'now') AS INTEGER) + 1"
            )
        conn.execute("""CREATE TABLE IF NOT EXISTS cdn_pending (
            uuid TEXT NOT NULL, period TEXT NOT NULL, bucket_end INTEGER NOT NULL,
            bytes INTEGER NOT NULL, PRIMARY KEY(uuid, period, bucket_end),
            FOREIGN KEY(uuid, period) REFERENCES cdn_usage(uuid, period) ON DELETE CASCADE)""")
        conn.execute("PRAGMA user_version=20")


def _upgrade_to_21(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(telegram_cleanup)")}
        if "pending_upload" not in columns:
            conn.execute(
                "ALTER TABLE telegram_cleanup ADD COLUMN pending_upload INTEGER NOT NULL DEFAULT 0"
            )
        conn.execute("PRAGMA user_version=21")


_MIGRATIONS = (
    (1, _upgrade_to_1),
    (2, _upgrade_to_2),
    (3, _upgrade_to_3),
    (4, _upgrade_to_4),
    (5, _upgrade_to_5),
    (6, _upgrade_to_6),
    (7, _upgrade_to_7),
    (8, _upgrade_to_8),
    (9, _upgrade_to_9),
    (10, _upgrade_to_10),
    (11, _upgrade_to_11),
    (12, _upgrade_to_12),
    (13, _upgrade_to_13),
    (14, _upgrade_to_14),
    (15, _upgrade_to_15),
    (16, _upgrade_to_16),
    (17, _upgrade_to_17),
    (18, _upgrade_to_18),
    (19, _upgrade_to_19),
    (20, _upgrade_to_20),
    (21, _upgrade_to_21),
)


def migrate(conn: sqlite3.Connection) -> None:
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current > SCHEMA_VERSION:
        raise RuntimeError(f"数据库版本 {current} 高于程序支持版本 {SCHEMA_VERSION}，请升级程序")
    for version, upgrade in _MIGRATIONS:
        if current < version:
            upgrade(conn)
