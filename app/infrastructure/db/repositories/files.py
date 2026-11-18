from __future__ import annotations

import secrets
import uuid
from collections.abc import Iterable
from typing import Any

from app.infrastructure.db.common import clean_name, row_dict, utcnow
from app.infrastructure.db.session import SQLiteSession
from app.services.categories import CATEGORY_LABELS, classify_file, effective_mime
from app.services.people import person_key


class FilesRepository(SQLiteSession):
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

    _summary_key: tuple[int, int] | None = None
    _summary: dict[str, Any]

    def library_summary(self) -> dict[str, Any]:
        """Reuse the workspace totals across page and search requests until a write."""
        with self._lock:
            # total_changes tracks our writes, data_version tracks other connections.
            version = self.conn.execute("PRAGMA data_version").fetchone()[0]
            key = (self.conn.total_changes, version)
            if self.conn.in_transaction:
                return {"categories": self.category_stats(), "stats": self.stats()}
            if self._summary_key != key:
                self._summary = {"categories": self.category_stats(), "stats": self.stats()}
                self._summary_key = key
            return self._summary

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
        return row_dict(row)

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
        name = clean_name(name)
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
            tuple(clean_name(segment, preserve_spaces=True) for segment in path) for path in paths
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
        *,
        upload_id: str | None = None,
    ) -> dict[str, Any]:
        """Create file metadata and, when supplied, complete its upload atomically."""
        name = clean_name(name)
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
            if upload_id is not None:
                cursor = self.conn.execute(
                    """UPDATE uploads SET state='done', progress=size, item_id=?, error=NULL,
                       updated_at=? WHERE id=? AND state='processing'""",
                    (item_id, now, upload_id),
                )
                if not cursor.rowcount:
                    raise LookupError("上传任务不存在或已不在处理中")
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
                name = clean_name(name)
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
