from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from pathlib import Path

from app.infrastructure.db.migrations import migrate
from app.services.person_names import chinese_person_name


class SQLiteSession:
    """Serialize connection access across the event loop and upload worker threads."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        try:
            self.conn.row_factory = sqlite3.Row
            self.conn.create_function(
                "person_display_name", 1, chinese_person_name, deterministic=True
            )
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.execute("PRAGMA busy_timeout=30000")
            self.conn.execute("PRAGMA journal_mode=WAL")
            migrate(self.conn)
        except BaseException:
            self.conn.close()
            raise

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

    def backup_to(self, target: sqlite3.Connection) -> None:
        if self.path == ":memory:":
            with self._lock:
                self.conn.backup(target)
            return
        uri = Path(self.path).resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=30)) as reader:
            reader.backup(target)

    def close(self) -> None:
        with self._lock:
            self.conn.close()
