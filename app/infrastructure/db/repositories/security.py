from __future__ import annotations

from app.infrastructure.db.session import SQLiteSession


class SecurityRepository(SQLiteSession):
    def login_failure_window(self, key: str, cutoff: float) -> tuple[int, float | None]:
        with self._lock, self.conn:
            # Prune all clients so expired keys cannot accumulate indefinitely.
            self.conn.execute("DELETE FROM login_failures WHERE occurred_at<=?", (cutoff,))
            row = self.conn.execute(
                "SELECT COUNT(*), MIN(occurred_at) FROM login_failures WHERE client_key=?",
                (key,),
            ).fetchone()
            return row[0], row[1]

    def record_login_failure(self, key: str, now: float, cutoff: float) -> None:
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM login_failures WHERE occurred_at<=?", (cutoff,))
            self.conn.execute(
                "INSERT INTO login_failures(client_key, occurred_at) VALUES (?,?)", (key, now)
            )

    def reset_login_failures(self, key: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM login_failures WHERE client_key=?", (key,))
