from __future__ import annotations

import json
import uuid
from typing import Any

from app.infrastructure.db.common import clean_name, row_dict, utcnow
from app.infrastructure.db.session import SQLiteSession


class UploadsRepository(SQLiteSession):
    def create_upload(
        self,
        parent_id: str | None,
        name: str,
        size: int,
        mime: str | None,
        temp_path: str,
        chunk_size: int,
        image_host: bool = False,
        *,
        import_id: str | None = None,
    ) -> dict[str, Any]:
        name = clean_name(name)
        if int(size) <= 0:
            raise ValueError("文件大小必须大于 0")
        if int(chunk_size) <= 0:
            raise ValueError("分片大小必须大于 0")
        upload_id = import_id or uuid.uuid4().hex
        now = utcnow()
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO uploads "
                "(id, parent_id, name, size, mime, temp_path, received, chunk_size, state, progress, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, ?, 'uploading', 0, ?, ?)",
                (upload_id, parent_id, name, int(size), mime, temp_path, int(chunk_size), now, now),
            )
            self.conn.execute(
                "UPDATE uploads SET image_host=?, link_import=?, received=? WHERE id=?",
                (int(image_host), int(import_id is not None), size if import_id else 0, upload_id),
            )
        return self.get_upload(upload_id)  # type: ignore[return-value]

    def get_upload(self, upload_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM uploads WHERE id = ?", (upload_id,)).fetchone()
        return row_dict(row)

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
        self, states: tuple[str, ...] = ("uploading", "error"), *, include_imports: bool = False
    ) -> list[dict[str, Any]]:
        """未完成的上传任务，供前端刷新后断点续传。"""
        if not states:
            return []
        placeholders = ",".join("?" for _ in states)
        with self._lock:
            rows = self.conn.execute(
                f"SELECT * FROM uploads WHERE state IN ({placeholders}) AND (link_import=0 OR ?) ORDER BY created_at DESC",
                (*states, include_imports),
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
                "SELECT * FROM uploads WHERE link_import=0 AND state IN ('uploading','error') AND updated_at < ?",
                (before,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_video_import(self) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute("SELECT data FROM video_import_batch WHERE id=1").fetchone()
            return json.loads(row[0]) if row else None

    def save_video_import(self, batch: dict[str, Any]) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO video_import_batch VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (json.dumps(batch, ensure_ascii=False),),
            )
