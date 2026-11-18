from __future__ import annotations

from typing import Any

from app.infrastructure.db.common import row_dict
from app.infrastructure.db.session import SQLiteSession


class AccountsRepository(SQLiteSession):
    def telegram_profiles(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(row) for row in self.conn.execute("SELECT * FROM telegram_profiles").fetchall()
            ]

    def active_telegram_profile(self) -> dict[str, Any] | None:
        with self._lock:
            return row_dict(
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

    def telegram_workspace_keys(self) -> list[str]:
        with self._lock:
            return [
                row[0]
                for row in self.conn.execute(
                    "SELECT DISTINCT database_key FROM telegram_workspaces"
                )
            ]

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
