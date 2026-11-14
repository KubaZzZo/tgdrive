from __future__ import annotations

import uuid
from typing import Any

from app.infrastructure.db.common import utcnow
from app.infrastructure.db.repositories.people import PeopleRepository
from app.services.categories import classify_file


class ChannelRepository(PeopleRepository):
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
