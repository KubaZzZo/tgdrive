from __future__ import annotations

import asyncio
import logging
import threading
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import anyio
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.infrastructure.db import Database
from app.services.cdn import CDNMonitor

logger = logging.getLogger("tgdrive.traffic")
TRAFFIC_TIMEZONE = ZoneInfo("Asia/Shanghai")


class TrafficMonitor:
    """Application payload estimates, buffered to avoid a SQLite write for every chunk."""

    def __init__(self, db: Database, *, enabled: bool = True):
        self.enabled = enabled
        self.db = db
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._pending: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
        self.cdn: CDNMonitor | None = None

    def record(self, source: Literal["web", "telegram"], inbound: int = 0, outbound: int = 0):
        if not self.enabled or not (inbound or outbound):
            return
        day = datetime.now(TRAFFIC_TIMEZONE).date().isoformat()
        with self._lock:
            values = self._pending[(day, source)]
            values[0] += inbound
            values[1] += outbound

    async def persist_periodically(self) -> None:
        while True:
            await asyncio.sleep(5)
            await anyio.to_thread.run_sync(self.flush)

    def _flush(self) -> None:
        with self._lock:
            pending = self._pending
            if not pending:
                return
            self._pending = defaultdict(lambda: [0, 0])
        try:
            self.db.record_traffic(
                [(day, source, *values) for (day, source), values in pending.items()]
            )
        except Exception:
            logger.exception("Could not persist traffic counters")
            with self._lock:
                for key, (inbound, outbound) in pending.items():
                    values = self._pending[key]
                    values[0] += inbound
                    values[1] += outbound

    def flush(self) -> None:
        with self._flush_lock:
            self._flush()

    def snapshot(self, now: datetime | None = None) -> dict:
        today = (now or datetime.now(UTC)).astimezone(TRAFFIC_TIMEZONE).date()
        day = today.isoformat()
        history = {
            (today - timedelta(days=offset)).isoformat(): {"inbound": 0, "outbound": 0}
            for offset in range(6, -1, -1)
        }
        sources = {key: {"inbound": 0, "outbound": 0} for key in ("web", "telegram")}
        # Serialize against flush so a pending batch is counted exactly once.
        with self._flush_lock:
            config = self.db.traffic_config()
            rows = self.db.traffic_rows(next(iter(history)))
            with self._lock:
                rows.extend(
                    {"day": date, "source": source, "inbound": values[0], "outbound": values[1]}
                    for (date, source), values in self._pending.items()
                )
        for row in rows:
            if row["day"] not in history or row["source"] not in sources:
                continue
            for field in ("inbound", "outbound"):
                history[row["day"]][field] += row[field]
                if row["day"] == day:
                    sources[row["source"]][field] += row[field]
        totals = history[day]
        used = totals["inbound"] + totals["outbound"]
        quota = config["quota_bytes"]
        return {
            "day": day,
            "timezone": TRAFFIC_TIMEZONE.key,
            "quota_bytes": quota,
            "started_at": config["started_at"],
            "sources": sources,
            "usage": {
                **totals,
                "used": used,
                "remaining": max(0, quota - used),
                "percent": round(used / quota * 100, 2),
                "warning": "exceeded"
                if used >= quota
                else "near"
                if used * 5 >= quota * 4
                else None,
                "history": [{"day": date, **values} for date, values in history.items()],
            },
            "cdn": self.cdn.snapshot() if self.cdn else None,
        }


class TrafficMiddleware:
    def __init__(self, app: ASGIApp, monitor: TrafficMonitor):
        self.app, self.monitor = app, monitor

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def receive_counted() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                self.monitor.record("web", inbound=len(message.get("body", b"")))
            return message

        async def send_counted(message: Message) -> None:
            await send(message)
            if message["type"] == "http.response.body":
                self.monitor.record("web", outbound=len(message.get("body", b"")))

        await self.app(scope, receive_counted, send_counted)
