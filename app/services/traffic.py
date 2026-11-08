from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.db import Database
from app.services.cdn import CDNMonitor

logger = logging.getLogger("tgdrive.traffic")


class TrafficMonitor:
    """Application payload estimates, buffered to avoid a SQLite write for every chunk."""

    def __init__(self, db: Database, host_state: Path):
        self.db = db
        self.host_state = host_state
        self._lock = threading.Lock()
        self._pending: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
        self._bytes = 0
        self._last_flush = time.monotonic()
        self.cdn: CDNMonitor | None = None

    def record(self, source: Literal["web", "telegram"], inbound: int = 0, outbound: int = 0):
        if not inbound and not outbound:
            return
        day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        with self._lock:
            values = self._pending[(day, source)]
            values[0] += inbound
            values[1] += outbound
            self._bytes += inbound + outbound
            if self._bytes >= 1024 * 1024 or time.monotonic() - self._last_flush >= 5:
                self._flush()

    def _flush(self) -> None:
        if not self._pending:
            return
        try:
            self.db.record_traffic(
                [(day, source, *values) for (day, source), values in self._pending.items()]
            )
        except Exception:
            # Monitoring must never interrupt a transfer. Retain the buffer for retry.
            logger.exception("Could not persist traffic counters")
            return
        self._pending.clear()
        self._bytes = 0
        self._last_flush = time.monotonic()

    def flush(self) -> None:
        with self._lock:
            self._flush()

    def snapshot(self, now: datetime | None = None) -> dict:
        self.flush()
        now = (now or datetime.now(UTC)).astimezone(ZoneInfo("Asia/Shanghai"))
        day = now.date().isoformat()
        config = self.db.traffic_config()
        sources = {key: {"inbound": 0, "outbound": 0} for key in ("web", "telegram")}
        for row in self.db.traffic_rows(day):
            if row["day"] == day:
                sources[row["source"]] = {key: row[key] for key in ("inbound", "outbound")}
        host = {"available": False, "stale": False, "error": "尚未安装 VPS 宿主机采集服务"}
        try:
            state = json.loads(self.host_state.read_text(encoding="utf-8"))
            updated = datetime.fromisoformat(state["updated_at"])
            age = (now - updated).total_seconds()
            if state["timezone"] != "Asia/Shanghai":
                raise ValueError("采集服务必须使用 Asia/Shanghai 时区")
            counters = state["days"].get(day, {"inbound": 0, "outbound": 0})
            if any(
                type(counters[k]) is not int or counters[k] < 0 for k in ("inbound", "outbound")
            ):
                raise ValueError("网卡计数无效")
            used = counters["inbound"] + counters["outbound"]
            quota = config["quota_bytes"]
            stale = age > 30 or age < -5
            host = {
                "available": True,
                "stale": stale,
                "error": "采集数据已过期，请检查宿主机采集服务" if stale else None,
                "updated_at": state["updated_at"],
                "started_at": state["started_at"],
                "interfaces": state["interfaces"],
                **counters,
                "used": used,
                "remaining": max(0, quota - used),
                "percent": round(used / quota * 100, 2),
                "warning": "exceeded" if used >= quota else "near" if used >= quota * 0.8 else None,
                "history": [
                    {"day": key, **value, "incomplete": key in state.get("incomplete_days", [])}
                    for key, value in sorted(state["days"].items())[-7:]
                ],
                "gaps": state.get("gaps", 0),
                "incomplete": day in state.get("incomplete_days", []),
            }
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            logger.warning("Host traffic state unavailable: %s", type(exc).__name__)
            host["error"] = "无法读取网卡统计，请检查宿主机采集服务"
        return {
            "day": day,
            "timezone": "Asia/Shanghai",
            "quota_bytes": config["quota_bytes"],
            "started_at": config["started_at"],
            "sources": sources,
            "host": host,
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
