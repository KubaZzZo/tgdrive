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
from app.infrastructure.db import Database

logger = logging.getLogger("tgdrive.cdn")
API_URL = "https://api.axisnow.io/client/v1/tenant_subscriptions/filter"
QUOTA_ZONE = ZoneInfo("Asia/Shanghai")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def subscription_rows(token: str) -> list[dict]:
    """Read-only filter API used by the AxisNow subscription console."""
    rows: list[dict] = []
    expected_total = None
    opener = urllib.request.build_opener(NoRedirect())
    for page in range(1, 101):
        request = urllib.request.Request(
            API_URL,
            method="POST",
            data=json.dumps({"page": page, "per_page": 100}).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                # The API edge rejects urllib's default Python client signature.
                "User-Agent": "TGDrive/2.1",
                "Accept": "application/json",
            },
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
        total = integer(data["result_info"]["total_count"])
        if expected_total is None:
            expected_total = total
        if total != expected_total or len(rows) > total:
            raise ValueError("订阅列表在分页期间发生变化")
        if len(rows) == total:
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
                observed_at = datetime.now(UTC)
                period = observed_at.astimezone(QUOTA_ZONE).strftime("%Y-%m")
                selected = {
                    s.strip()
                    for s in self.settings.axisnow_subscription_ids.split(",")
                    if s.strip()
                }
                subscriptions = {}
                for row in raw:
                    subscription_id = row["uuid"]
                    if (
                        not isinstance(subscription_id, str)
                        or not subscription_id.strip()
                        or subscription_id in subscriptions
                    ):
                        raise ValueError("无效的订阅 ID")
                    subscriptions[subscription_id] = row

                active = {}
                if selected <= subscriptions.keys():
                    for subscription_id, row in subscriptions.items():
                        if selected and subscription_id not in selected:
                            continue
                        status = row.get("status")
                        if not isinstance(status, str) or not status.strip():
                            raise ValueError("无效的订阅状态")
                        if status == "active":
                            active[subscription_id] = row
                if not active or (selected and selected != active.keys()):
                    # A complete listing confirms the scope has changed even
                    # when usage data on another subscription is malformed.
                    self.rows = []
                    self.updated_at = observed_at
                    self.error = (
                        "指定订阅已停用或不在列表中，请核对 AXISNOW_SUBSCRIPTION_IDS"
                        if selected
                        else "平台当前没有有效订阅，请核对 AxisNow 订阅状态"
                    )
                    return

                rows = []
                observations = []
                for subscription_id, row in active.items():
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
                    if (
                        row["quota_period"] == period
                        and (updated - observed_at).total_seconds() <= 60
                    ):
                        observations.append((subscription_id, period, used, updated.timestamp()))
                    rows.append(
                        {
                            "id": subscription_id,
                            "name": row.get("share_code") or subscription_id,
                            "period": row["quota_period"],
                            "quota_bytes": quota,
                            "provider_used": used,
                            "usage_updated_at": updated.isoformat(),
                            "over_quota": row.get("over_quota") is True,
                        }
                    )
                self.db.cdn_observe(observations, observed_at.timestamp())
                self.rows, self.updated_at, self.error = rows, now, None
            except Exception as exc:
                # Never log response bodies or exception strings containing credentials.
                logger.warning("AxisNow quota refresh failed: %s", type(exc).__name__)
                if isinstance(exc, urllib.error.HTTPError) and exc.code in {401, 403}:
                    self.error = "CDN 令牌无效或无读取权限"
                elif isinstance(exc, urllib.error.HTTPError) and 500 <= exc.code < 600:
                    self.error = "AxisNow API 暂时不可用，系统将自动重试"
                else:
                    self.error = "CDN 用量同步失败，请检查 API 配置或网络"

    def snapshot(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(UTC)
        period = now.astimezone(QUOTA_ZONE).strftime("%Y-%m")
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
        if status["stale"] or not status["available"]:
            detail = "CDN 用量数据暂不可用，媒体传输已暂停，请查看流量面板"
        elif status["blocked"]:
            detail = "CDN 用量已达到保护阈值或平台额度，媒体传输已暂停"
        elif cannot_fit:
            detail = "本次请求所需流量超过 CDN 保护余量"
        else:
            return
        raise HTTPException(
            429,
            detail,
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
            raise HTTPException(
                429,
                "CDN 保护余量不足，传输已暂停",
                headers={"Retry-After": "60", "Cache-Control": "no-store"},
            )
