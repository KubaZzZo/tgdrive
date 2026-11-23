from __future__ import annotations

import json
import re
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from time import monotonic
from zipfile import BadZipFile, ZipFile

from app.infrastructure.db.common import clean_name
from app.infrastructure.db.repositories.index_restore import (
    RESTORE_COLUMNS,
    Tables,
    by_item,
    row_key,
    select_rows,
)
from app.services.image_host import IMAGE_MIMES

MAX_ARCHIVE = 64 * 1024**2
MAX_INDEX = 192 * 1024**2
MAX_ROWS = 100_000
# Versions 19 through 21 leave the exported index tables unchanged.
INDEX_SCHEMA_VERSIONS = {18, 19, 20, 21}
ITEM_ID = re.compile(r"[0-9a-f]{32}")
TOKEN = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _date(value: str) -> None:
    if not isinstance(value, str) or datetime.fromisoformat(value).utcoffset() is None:
        raise ValueError("备份中的日期无效")


def _positive(value: object) -> bool:
    return type(value) is int and value > 0


def _validate_rows(tables: Tables, chat_id: str) -> None:
    parts = by_item(tables["file_parts"])
    channel_rows = by_item(tables["channel_messages"])
    bot_items = {row["item_id"] for row in tables["channel_bot_files"]}
    item_ids = {item["id"] for item in tables["items"]}
    for item in tables["items"]:
        item_id = item["id"]
        if not isinstance(item_id, str) or not ITEM_ID.fullmatch(item_id):
            raise ValueError("备份中的文件 ID 无效")
        clean_name(item["name"], preserve_spaces=True)
        _date(item["created_at"])
        _date(item["updated_at"])
        if (
            item["kind"] not in {"folder", "file"}
            or type(item["size"]) is not int
            or item["size"] < 0
        ):
            raise ValueError("备份中的文件类型或大小无效")
        if item["kind"] == "file" and item["tg_chat_id"] != chat_id:
            raise ValueError("备份包含其他 Telegram 频道的文件")
        mime = item["mime"]
        if mime is not None and not re.fullmatch(r"[a-zA-Z0-9.+_-]+/[a-zA-Z0-9.+_-]+", mime):
            raise ValueError("备份中的媒体类型无效")
        segments = parts.get(item_id, [])
        if segments and item["kind"] != "file":
            raise ValueError("目录包含文件分段")
        if segments and (
            any(
                row["part_index"] != index
                or not _positive(row["tg_message_id"])
                or not _positive(row["size"])
                for index, row in enumerate(segments)
            )
            or sum(row["size"] for row in segments) != item["size"]
        ):
            raise ValueError("备份中的 Telegram 分段不完整")
        if (
            item["kind"] == "file"
            and item["size"]
            and not segments
            and (item_id not in bot_items or not channel_rows.get(item_id))
        ):
            raise ValueError("备份中的非空文件缺少 Telegram 分段")

    for table, rows in tables.items():
        keys = set()
        for row in rows:
            key = row_key(table, row)
            if any(value is None for value in key) or key in keys:
                raise ValueError(f"备份中的 {table} 索引有重复或空键")
            keys.add(key)
            if table == "channel_messages" and (
                row["chat_id"] != chat_id or not _positive(row["message_id"])
            ):
                raise ValueError("备份中的频道消息索引无效")
            if row.get("item_id") is not None and row["item_id"] not in item_ids:
                raise ValueError(f"备份中的 {table} 关联不到文件")
            if table in {"shares", "image_hosts"} and not TOKEN.fullmatch(row["token"]):
                raise ValueError("备份中的公开链接无效")
            if table == "image_hosts" and row["mime"] not in IMAGE_MIMES:
                raise ValueError("备份中的图床类型无效")
            if table == "shares" and row["expires_at"] is not None:
                _date(row["expires_at"])


def read_index_archive(archive: Path, directory: Path, chat_id: str) -> tuple[dict, Tables]:
    path = directory / "restore.sqlite3"
    try:
        with ZipFile(archive) as backup:
            if sorted(backup.namelist()) != ["index.sqlite3", "manifest.json"]:
                raise ValueError("请选择 TG Drive 导出的频道索引 ZIP")
            if backup.getinfo("manifest.json").file_size > 16 * 1024:
                raise ValueError("备份清单过大")
            manifest = json.loads(backup.read("manifest.json"))
            if (
                not isinstance(manifest, dict)
                or manifest.get("format") != "tgdrive-channel-index"
                or manifest.get("format_version") != 1
                or type(manifest.get("source_schema_version")) is not int
                or manifest.get("source_schema_version") not in INDEX_SCHEMA_VERSIONS
                or not isinstance(manifest.get("app_version"), str)
            ):
                raise ValueError("不支持的频道索引备份版本")
            if manifest.get("channel_id") != chat_id:
                raise ValueError("备份频道与当前频道不同")
            if backup.getinfo("index.sqlite3").file_size > MAX_INDEX:
                raise ValueError("解压后的 SQLite 索引超过 192 MiB")
            with backup.open("index.sqlite3") as source, path.open("wb") as target:
                shutil.copyfileobj(source, target, 256 * 1024)

        with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as reader:
            reader.row_factory = sqlite3.Row
            reader.execute("PRAGMA trusted_schema=OFF")
            reader.execute("PRAGMA query_only=ON")
            reader.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1_000_000)
            deadline = monotonic() + 8
            reader.set_progress_handler(lambda: int(monotonic() >= deadline), 1000)
            if (
                reader.execute("PRAGMA user_version").fetchone()[0]
                != manifest["source_schema_version"]
            ):
                raise ValueError("备份 SQLite 版本不匹配")
            real_tables = {
                row["name"]
                for row in reader.execute("PRAGMA table_list")
                if row["schema"] == "main" and row["type"] == "table"
            }
            for table, columns in RESTORE_COLUMNS.items():
                actual = {
                    row["name"]
                    for row in reader.execute(f"PRAGMA table_xinfo({table})")
                    if not row["hidden"]
                }
                if table not in real_tables or not set(columns).issubset(actual):
                    raise ValueError(f"备份缺少 {table} 的标准表结构")

            def authorize(action, table, column, database, source):
                if source is None and (
                    action == sqlite3.SQLITE_SELECT
                    or (
                        action == sqlite3.SQLITE_READ
                        and database == "main"
                        and table in RESTORE_COLUMNS
                    )
                ):
                    return sqlite3.SQLITE_OK
                return sqlite3.SQLITE_DENY

            reader.set_authorizer(authorize)
            tables: Tables = {}
            total = 0
            for table in RESTORE_COLUMNS:
                tables[table] = []
                for row in select_rows(reader, table):
                    total += 1
                    if total > MAX_ROWS:
                        raise ValueError("备份索引超过 100,000 条记录")
                    if any(not isinstance(value, (str, int, type(None))) for value in row):
                        raise ValueError("备份包含不支持的数据类型")
                    tables[table].append(dict(row))
    except (BadZipFile, NotImplementedError, RuntimeError, sqlite3.Error, UnicodeError) as exc:
        raise ValueError("无效的频道索引 ZIP 或 SQLite 文件") from exc
    try:
        _validate_rows(tables, chat_id)
    except (AttributeError, TypeError) as exc:
        raise ValueError("备份中的索引字段类型无效") from exc
    return manifest, tables
