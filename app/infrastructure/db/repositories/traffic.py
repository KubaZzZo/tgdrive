from __future__ import annotations

from typing import Any

from app.infrastructure.db.session import SQLiteSession


class TrafficRepository(SQLiteSession):
    def record_traffic(self, rows: list[tuple[str, str, int, int]]) -> None:
        with self._lock, self.conn:
            self.conn.executemany(
                """INSERT INTO traffic_daily(day,source,inbound,outbound) VALUES (?,?,?,?)
                ON CONFLICT(day,source) DO UPDATE SET
                inbound=inbound+excluded.inbound, outbound=outbound+excluded.outbound""",
                rows,
            )

    def traffic_config(self) -> dict[str, Any]:
        with self._lock:
            return dict(self.conn.execute("SELECT * FROM traffic_config WHERE id=1").fetchone())

    def update_traffic_config(self, quota_bytes: int) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE traffic_config SET quota_bytes=? WHERE id=1",
                (quota_bytes,),
            )

    def traffic_rows(self, since: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(row)
                for row in self.conn.execute(
                    "SELECT * FROM traffic_daily WHERE day>=? ORDER BY day", (since,)
                ).fetchall()
            ]
