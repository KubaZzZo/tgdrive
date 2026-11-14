from __future__ import annotations

import json
from collections.abc import Iterable
from unicodedata import normalize

from app.infrastructure.db.repositories.files import FilesRepository
from app.services.people import MAX_PERSON_LENGTH, person_key
from app.services.person_names import chinese_person_name

PEOPLE_AI_MAX_ATTEMPTS = 3


class PeopleRepository(FilesRepository):
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
        if not file.get("people_enabled", True):
            return False
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
