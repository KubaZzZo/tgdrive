from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from typing import Any

from app.infrastructure.db.session import SQLiteSession

# Explicit import schema for the v1 channel index. Control tables and scan cursors
# never participate in the merge.
RESTORE_COLUMNS = {
    "items": (
        "id",
        "parent_id",
        "kind",
        "name",
        "size",
        "mime",
        "sha256",
        "tg_chat_id",
        "created_at",
        "updated_at",
        "category",
        "source",
        "trash_root",
        "trashed_at",
        "trash_parent_id",
    ),
    "file_parts": ("item_id", "part_index", "tg_message_id", "size"),
    "channel_messages": (
        "chat_id",
        "message_id",
        "item_id",
        "state",
        "media_key",
        "scan_run",
        "remote_version",
        "remote_name",
        "people_ai_key",
    ),
    "channel_bot_files": ("item_id", "bot_id", "file_id", "remote_version", "size"),
    "channel_bot_covers": ("item_id", "file_id"),
    "people_labels": ("person_key", "name", "include_automatic"),
    "video_people": ("item_id", "person_key", "name", "display_name", "aliases"),
    "video_people_edits": ("item_id", "person_key", "assigned"),
    "shares": ("token", "item_id", "created_at", "expires_at"),
    "image_hosts": ("item_id", "token", "mime", "created_at"),
}

RESTORE_KEYS = {
    "items": ("id",),
    "file_parts": ("item_id", "part_index"),
    "channel_messages": ("chat_id", "message_id"),
    "channel_bot_files": ("item_id",),
    "channel_bot_covers": ("item_id",),
    "people_labels": ("person_key",),
    "video_people": ("item_id", "person_key"),
    "video_people_edits": ("item_id", "person_key"),
    "shares": ("token",),
    "image_hosts": ("item_id",),
}
PUBLIC_TABLES = {"shares", "image_hosts"}

Row = dict[str, Any]
Tables = dict[str, list[Row]]


class RestoreChanged(ValueError):
    pass


def row_key(table: str, row: Row) -> tuple:
    return tuple(row[column] for column in RESTORE_KEYS[table])


def select_rows(connection, table: str):
    columns = ",".join(RESTORE_COLUMNS[table])
    order = ",".join(RESTORE_KEYS[table])
    return connection.execute(f"SELECT {columns} FROM main.{table} ORDER BY {order}")


def by_item(rows: list[Row]) -> dict[str, list[Row]]:
    result = defaultdict(list)
    for row in rows:
        result[row["item_id"]].append(row)
    return result


class RestorePlan:
    def __init__(
        self, source: Tables, live: Tables, cleanup: set[int], chat_id: str, include_links: bool
    ):
        self.source, self.live = source, live
        self.additions: Tables = {table: [] for table in RESTORE_COLUMNS}
        self.conflicts: list[tuple[str, str, str]] = []
        self.unchanged = 0
        self.trash = 0
        self.skipped_links = 0
        self.available: set[str] = set()
        self._plan_items(cleanup, chat_id)
        self._plan_references(include_links)

    def _plan_items(self, cleanup: set[int], chat_id: str) -> None:
        live_items = {item["id"]: item for item in self.live["items"]}
        live_names = {
            (item["parent_id"], item["name"])
            for item in live_items.values()
            if item["trash_root"] is None
        }
        source_parts = by_item(self.source["file_parts"])
        live_parts = by_item(self.live["file_parts"])
        source_messages = by_item(self.source["channel_messages"])
        message_owner = {
            row["tg_message_id"]: row["item_id"]
            for row in self.live["file_parts"]
            if live_items.get(row["item_id"], {}).get("tg_chat_id") == chat_id
        }
        message_owner.update(
            (row["message_id"], row["item_id"])
            for row in self.live["channel_messages"]
            if row["item_id"] and row["chat_id"] == chat_id
        )
        children = defaultdict(list)
        for item in self.source["items"]:
            children[item["parent_id"]].append(item)
        queue = deque(children[None])
        seen = set()
        available_folders = {None}
        while queue:
            item = queue.popleft()
            item_id = item["id"]
            seen.add(item_id)
            queue.extend(children[item_id])
            if item["trash_root"] is not None:
                self.trash += 1
                continue
            live = live_items.get(item_id)
            parts = source_parts.get(item_id, [])
            messages = {row["tg_message_id"] for row in parts}
            messages.update(row["message_id"] for row in source_messages.get(item_id, []))
            reason = ""
            if item["parent_id"] not in available_folders:
                reason = "父目录缺失或有冲突"
            elif live:
                if live != item or live_parts.get(item_id, []) != parts:
                    reason = "已有条目内容或位置不同"
                else:
                    self.unchanged += 1
            elif (item["parent_id"], item["name"]) in live_names:
                reason = "目标目录已有同名条目"
            elif messages.intersection(cleanup):
                reason = "Telegram 分段仍在删除队列"
            elif any(mid in message_owner and message_owner[mid] != item_id for mid in messages):
                reason = "Telegram 消息已被其他条目使用"
            else:
                self.additions["items"].append(item)
                self.additions["file_parts"].extend(parts)
                message_owner.update(dict.fromkeys(messages, item_id))
            if reason:
                self.conflicts.append((item_id, item["name"], reason))
            else:
                self.available.add(item_id)
                if item["kind"] == "folder":
                    available_folders.add(item_id)
        for item in self.source["items"]:
            if item["id"] not in seen:
                self.conflicts.append((item["id"], item["name"], "目录结构不完整"))

    def _plan_references(self, include_links: bool) -> None:
        image_tokens = {row["token"]: row["item_id"] for row in self.live["image_hosts"]}
        bot_files = {row["item_id"]: row for row in self.live["channel_bot_files"]}
        source_bot_files = {row["item_id"]: row for row in self.source["channel_bot_files"]}
        for table, rows in self.source.items():
            if table in {"items", "file_parts"}:
                continue
            if table in PUBLIC_TABLES and not include_links:
                self.skipped_links += len(rows)
                continue
            existing = {row_key(table, row): row for row in self.live[table]}
            for row in rows:
                item_id = row.get("item_id")
                if table != "people_labels" and item_id not in self.available:
                    continue
                key = row_key(table, row)
                current = existing.get(key)
                if current == row:
                    continue
                if table == "channel_bot_covers":
                    source_file = source_bot_files.get(item_id)
                    if not source_file or bot_files.get(item_id) != source_file:
                        continue
                if current or (table == "image_hosts" and row["token"] in image_tokens):
                    reference = item_id or str(key[0])
                    self.conflicts.append((reference, table, "关联记录与当前索引冲突"))
                    continue
                self.additions[table].append(row)
                if table == "channel_bot_files":
                    bot_files[item_id] = row
                if table == "image_hosts":
                    image_tokens[row["token"]] = item_id

    def preview(self, archive_digest: str, include_links: bool) -> dict:
        fingerprint = hashlib.sha256(
            json.dumps(
                [archive_digest, include_links, self.additions, self.conflicts, self.unchanged],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return {
            "preview_id": fingerprint,
            "added": {table: len(rows) for table, rows in self.additions.items()},
            "unchanged_items": self.unchanged,
            "conflict_count": len(self.conflicts),
            "conflicts": [
                {"name": name, "reason": reason} for _, name, reason in self.conflicts[:25]
            ],
            "skipped_trash": self.trash,
            "skipped_links": self.skipped_links,
        }


class IndexRestoreRepository(SQLiteSession):
    def restore_channel_index(
        self,
        source: Tables,
        chat_id: str,
        archive_digest: str,
        include_links: bool,
        expected_preview: str | None,
    ) -> dict:
        with self._lock, self.conn:
            self.conn.execute("BEGIN IMMEDIATE" if expected_preview else "BEGIN")
            live = {
                table: [dict(row) for row in select_rows(self.conn, table)]
                for table in RESTORE_COLUMNS
            }
            cleanup = {
                row[0]
                for row in self.conn.execute(
                    "SELECT message_id FROM telegram_cleanup WHERE chat_id=?", (chat_id,)
                )
            }
            plan = RestorePlan(source, live, cleanup, chat_id, include_links)
            result = plan.preview(archive_digest, include_links)
            if expected_preview is not None:
                if result["preview_id"] != expected_preview:
                    raise RestoreChanged("备份或目标索引已变化，请重新预览")
                for table, columns in RESTORE_COLUMNS.items():
                    fields = ",".join(columns)
                    placeholders = ",".join("?" for _ in columns)
                    self.conn.executemany(
                        f"INSERT INTO {table}({fields}) VALUES({placeholders})",
                        (tuple(row[column] for column in columns) for row in plan.additions[table]),
                    )
            return result
