from __future__ import annotations

import time

from app.infrastructure.db.session import SQLiteSession

PROVIDER_LAG_SECONDS = 300
CHARGE_BUCKET_SECONDS = 60
SQLITE_MAX_INTEGER = 2**63 - 1


class CDNRepository(SQLiteSession):
    def cdn_observe(self, rows: list[tuple[str, str, int, float]], observed_at: float) -> None:
        with self._lock, self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            for uuid, period, used, provider_at in rows:
                key = (uuid, period)
                self.conn.execute(
                    """INSERT INTO cdn_usage (uuid, period, charged) VALUES (?, ?, 0)
                    ON CONFLICT(uuid, period) DO NOTHING""",
                    key,
                )
                current = self.conn.execute(
                    "SELECT charged, provider_used, provider_at, legacy_at FROM cdn_usage "
                    "WHERE uuid=? AND period=?",
                    key,
                ).fetchone()
                watermark, legacy_at = current["provider_at"], current["legacy_at"]
                # Only a newer provider watermark can retire reservations. A poll
                # time is never evidence that the provider has counted those bytes.
                if (
                    (watermark is None or provider_at > watermark)
                    and used >= current["provider_used"]
                    and provider_at <= observed_at
                ):
                    watermark = provider_at
                    cutoff = provider_at - PROVIDER_LAG_SECONDS
                    self.conn.execute(
                        "DELETE FROM cdn_pending WHERE uuid=? AND period=? AND bucket_end<=?",
                        (*key, cutoff),
                    )
                    if legacy_at is not None and cutoff >= legacy_at:
                        legacy_at = None
                pending = self.conn.execute(
                    "SELECT COALESCE(SUM(bytes), 0) FROM cdn_pending WHERE uuid=? AND period=?",
                    key,
                ).fetchone()[0]
                provider_used = max(used, current["provider_used"])
                charged = min(provider_used + pending, SQLITE_MAX_INTEGER)
                if legacy_at is not None:
                    charged = max(charged, current["charged"])
                self.conn.execute(
                    """UPDATE cdn_usage SET charged=?, provider_used=?, provider_at=?, legacy_at=?
                    WHERE uuid=? AND period=?""",
                    (charged, provider_used, watermark, legacy_at, *key),
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
            bucket_end = (int(time.time()) // CHARGE_BUCKET_SECONDS + 1) * CHARGE_BUCKET_SECONDS
            for uuid, period, ceiling in limits:
                row = self.conn.execute(
                    "SELECT charged FROM cdn_usage WHERE uuid=? AND period=?",
                    (uuid, period),
                ).fetchone()
                if row is None or row[0] + size > ceiling:
                    return False
            for uuid, period, _ in limits:
                self.conn.execute(
                    """INSERT INTO cdn_pending (uuid, period, bucket_end, bytes) VALUES (?, ?, ?, ?)
                    ON CONFLICT(uuid, period, bucket_end) DO UPDATE SET bytes=bytes+excluded.bytes""",
                    (uuid, period, bucket_end, size),
                )
                self.conn.execute(
                    "UPDATE cdn_usage SET charged=charged+? WHERE uuid=? AND period=?",
                    (size, uuid, period),
                )
            return True
