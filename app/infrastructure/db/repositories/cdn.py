from __future__ import annotations

from app.infrastructure.db.session import SQLiteSession


class CDNRepository(SQLiteSession):
    def cdn_observe(self, rows: list[tuple[str, str, int]]) -> None:
        with self._lock, self.conn:
            self.conn.executemany(
                """INSERT INTO cdn_usage VALUES (?,?,?) ON CONFLICT(uuid,period)
                DO UPDATE SET charged=MAX(charged,excluded.charged)""",
                rows,
            )

    def cdn_totals(self) -> dict[tuple[str, str], int]:
        with self._lock:
            return {
                (r["uuid"], r["period"]): r["charged"]
                for r in self.conn.execute("SELECT * FROM cdn_usage")
            }

    def cdn_charge(self, limits: list[tuple[str, str, int]], size: int) -> bool:
        # One transaction protects concurrent streams and separate application workers.
        with self._lock, self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            for uuid, period, ceiling in limits:
                row = self.conn.execute(
                    "SELECT charged FROM cdn_usage WHERE uuid=? AND period=?",
                    (uuid, period),
                ).fetchone()
                if row is None or row[0] + size > ceiling:
                    return False
            for uuid, period, _ in limits:
                self.conn.execute(
                    "UPDATE cdn_usage SET charged=charged+? WHERE uuid=? AND period=?",
                    (size, uuid, period),
                )
            return True
