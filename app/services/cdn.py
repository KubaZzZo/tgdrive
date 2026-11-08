from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from contextlib import suppress
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from app.config import Settings
from app.db import Database

logger = logging.getLogger("tgdrive.cdn")
API_URL = "https://api.axisnow.io/client/v1/tenant_subscriptions/filter"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def subscription_rows(token: str) -> list[dict]:
    """Read-only filter API used by the AxisNow subscription console."""
    rows: list[dict] = []
    opener = urllib.request.build_opener(NoRedirect())
    for page in range(1, 101):
        request = urllib.request.Request(
            API_URL,
            method="POST",
            data=json.dumps({"page": page, "per_page": 100}).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with opener.open(request, timeout=10) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("响应过大")
        data = json.loads(raw)
        if data.get("success") is not True or not isinstance(data.get("result"), list):
            raise ValueError("无效的订阅响应")
        batch = data["result"]
        rows.extend(batch)
        if len(rows) >= int(data["result_info"]["total_count"]):
            if len({row["uuid"] for row in rows}) != len(rows):
                raise ValueError("订阅分页重复")
            return rows
        if not batch:
            break
    raise ValueError("订阅分页不完整")


def integer(value) -> int:
    if isinstance(value, bool) or not str(value).isdigit():
        raise ValueError("无效的流量计数")
    number = int(value)
    if number > 9_000_000_000_000_000_000:
        raise ValueError("流量计数超出范围")
    return number


class CDNMonitor:
    def __init__(self, db: Database, settings: Settings):
        self.db, self.settings = db, settings
        self.rows: list[dict] = []
        self.updated_at: datetime | None = None
        self.error: str | None = None
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._attempted_at: datetime | None = None

    @property
    def configured(self) -> bool:
        return bool(self.settings.axisnow_api_token)

    async def start(self) -> None:
        if self.configured:
            self._task = asyncio.create_task(self._poll())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    async def _poll(self) -> None:
        while True:
            await self.refresh()
            await asyncio.sleep(60)

    async def refresh(self) -> None:
        if not self.configured:
            return
        async with self._lock:
            now = datetime.now(UTC)
            # Requests can join the initial background fetch without refetching.
            if self._attempted_at and (now - self._attempted_at).total_seconds() < 30:
                return
            self._attempted_at = now
            try:
                raw = await asyncio.to_thread(subscription_rows, self.settings.axisnow_api_token)
                selected = {
                    s.strip()
                    for s in self.settings.axisnow_subscription_ids.split(",")
                    if s.strip()
                }
                rows = []
                for row in raw:
                    if selected and row["uuid"] not in selected:
                        continue
                    if row["status"] != "active":
                        continue
                    mode = row["effective_quota_mode"]
                    if mode not in {"fixed", "unlimited"}:
                        raise ValueError("未知的额度模式")
                    quota = (
                        integer(row["effective_quota_mb"]) * 1024 * 1024
                        if mode == "fixed"
                        else None
                    )
                    if quota is not None and (quota <= 0 or quota > 9_000_000_000_000_000_000):
                        raise ValueError("无效的订阅额度")
                    used = integer(row["used_bytes"])
                    updated = datetime.fromisoformat(row["usage_updated_at"].replace("Z", "+00:00"))
                    if updated.tzinfo is None:
                        raise ValueError("用量时间缺少时区")
                    rows.append(
                        {
                            "id": row["uuid"],
                            "name": row.get("share_code") or row["uuid"],
                            "period": row["quota_period"],
                            "quota_bytes": quota,
                            "provider_used": used,
                            "usage_updated_at": updated.isoformat(),
                            "over_quota": row.get("over_quota") is True,
                        }
                    )
                if not rows or (selected and selected != {r["id"] for r in rows}):
                    raise ValueError("没有找到全部有效订阅，请检查订阅范围")
                self.db.cdn_observe([(r["id"], r["period"], r["provider_used"]) for r in rows])
                self.rows, self.updated_at, self.error = rows, now, None
            except Exception as exc:
                # Never log response bodies or exception strings containing credentials.
                logger.warning("AxisNow quota refresh failed: %s", type(exc).__name__)
                self.error = (
                    "CDN 令牌无效或无读取权限"
                    if isinstance(exc, urllib.error.HTTPError) and exc.code in {401, 403}
                    else "CDN 用量同步失败，请检查 API 配置或网络"
                )

    def snapshot(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(UTC)
        period = now.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m")
        stale = self.updated_at is None or (now - self.updated_at).total_seconds() > 180
        totals = self.db.cdn_totals() if self.configured else {}
        subscriptions = []
        for row in self.rows:
            age = (now - datetime.fromisoformat(row["usage_updated_at"])).total_seconds()
            # usage_updated_at can remain unchanged on an idle subscription.
            # A recent successful API read confirms it; age alone must not lock
            # an idle site out of producing its next usage update.
            row_stale = row["period"] != period or age < -60
            used = max(row["provider_used"], totals.get((row["id"], row["period"]), 0))
            quota = row["quota_bytes"]
            percent = used / quota * 100 if quota else None
            blocked = row["over_quota"] or (
                percent is not None and percent >= self.settings.cdn_stop_percent
            )
            subscriptions.append(
                {
                    **row,
                    "used": used,
                    "remaining": max(0, quota - used) if quota else None,
                    "percent": round(percent, 2) if percent is not None else None,
                    "stale": row_stale,
                    "blocked": blocked,
                    "warning": blocked
                    or (percent is not None and percent >= self.settings.cdn_warning_percent),
                }
            )
        stale = stale or any(row["stale"] for row in subscriptions)
        blocked = self.configured and (
            stale or not subscriptions or any(row["blocked"] for row in subscriptions)
        )
        return {
            "configured": self.configured,
            "available": bool(subscriptions),
            "stale": stale,
            "blocked": blocked,
            "warning": blocked or any(row["warning"] for row in subscriptions),
            "error": self.error,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "warning_percent": self.settings.cdn_warning_percent,
            "stop_percent": self.settings.cdn_stop_percent,
            "subscriptions": subscriptions,
        }

    async def check(self, size: int = 0) -> None:
        if not self.configured:
            return
        if self.updated_at is None or (datetime.now(UTC) - self.updated_at).total_seconds() > 60:
            await self.refresh()
        status = self.snapshot()
        cannot_fit = any(
            row["quota_bytes"] is not None
            and row["used"] + size > row["quota_bytes"] * self.settings.cdn_stop_percent // 100
            for row in status["subscriptions"]
        )
        if status["blocked"] or cannot_fit:
            raise HTTPException(
                429,
                "CDN 流量保护已开启：余量不足或用量数据过期，请查看流量面板",
                headers={"Retry-After": "60", "Cache-Control": "no-store"},
            )

    async def charge(self, size: int) -> None:
        await self.check()
        if not self.configured or not size:
            return
        limits = [
            (r["id"], r["period"], r["quota_bytes"] * self.settings.cdn_stop_percent // 100)
            for r in self.rows
            if r["quota_bytes"] is not None
        ]
        if not self.db.cdn_charge(limits, size):
            raise HTTPException(429, "CDN 保护余量不足，传输已暂停", headers={"Retry-After": "60"})
