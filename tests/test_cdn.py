from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from conftest import make_settings
from fastapi import HTTPException

from app.db import Database
from app.services.cdn import CDNMonitor, subscription_rows


def row(used=0, uuid="limited", mode="fixed"):
    now = datetime.now(UTC)
    return {
        "uuid": uuid,
        "status": "active",
        "share_code": uuid,
        "effective_quota_mode": mode,
        "effective_quota_mb": 1,
        "used_bytes": str(used),
        "over_quota": False,
        "quota_period": now.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m"),
        "usage_updated_at": now.isoformat(),
    }


async def refresh(monitor, rows):
    monitor._attempted_at = None
    with patch("app.services.cdn.subscription_rows", return_value=rows):
        await monitor.refresh()


def test_cdn_counts_persist_and_never_reset_on_lagging_api(workdir):
    async def run():
        path = workdir / "cdn.db"
        db = Database(path)
        settings = make_settings(workdir, axisnow_api_token="test-token")
        monitor = CDNMonitor(db, settings)
        await refresh(monitor, [row(100), row(800, "unlimited", "unlimited")])
        await monitor.charge(250)
        await refresh(monitor, [row(200), row(800, "unlimited", "unlimited")])
        assert monitor.snapshot()["subscriptions"][0]["used"] == 350
        assert not monitor.snapshot()["blocked"]
        db.close()
        db = Database(path)
        monitor = CDNMonitor(db, settings)
        await refresh(monitor, [row(200)])
        assert monitor.snapshot()["subscriptions"][0]["used"] == 350
        await monitor.charge(50)
        assert monitor.snapshot()["subscriptions"][0]["used"] == 400
        assert "test-token" not in str(monitor.snapshot())
        assert "test-token" not in repr(settings)
        db.close()

    asyncio.run(run())


def test_warn_stop_and_preflight_preserve_reserve(workdir):
    async def run():
        db = Database(workdir / "cdn.db")
        monitor = CDNMonitor(db, make_settings(workdir, axisnow_api_token="test-token"))
        await refresh(monitor, [row(850_000)])
        assert monitor.snapshot()["warning"]
        assert not monitor.snapshot()["blocked"]
        with pytest.raises(HTTPException) as error:
            await monitor.check(200_000)
        assert error.value.status_code == 429
        assert db.cdn_totals()[("limited", row()["quota_period"])] == 850_000
        await monitor.charge(100_000)
        with pytest.raises(HTTPException):
            await monitor.charge(100_000)
        await refresh(monitor, [row(1_000_000)])
        assert monitor.snapshot()["blocked"]
        db.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "condition", ["month", "source_future", "bad_counter", "missing", "api_stale"]
)
def test_uncertain_quota_blocks_media(workdir, condition):
    async def run():
        db = Database(workdir / "cdn.db")
        monitor = CDNMonitor(db, make_settings(workdir, axisnow_api_token="test-token"))
        data = row()
        if condition == "month":
            data["quota_period"] = "2000-01"
        if condition == "source_future":
            data["usage_updated_at"] = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        if condition == "bad_counter":
            data["used_bytes"] = "NaN"
        await refresh(monitor, [] if condition == "missing" else [data])
        if condition == "api_stale":
            monitor.updated_at = datetime.now(UTC) - timedelta(minutes=4)
        with pytest.raises(HTTPException) as error:
            await monitor.check()
        assert error.value.status_code == 429
        db.close()

    asyncio.run(run())


def test_unlimited_and_disabled_monitors_do_not_block(workdir):
    async def run():
        db = Database(workdir / "cdn.db")
        disabled = CDNMonitor(db, make_settings(workdir))
        await disabled.charge(100_000_000)
        assert not disabled.snapshot()["blocked"]
        monitor = CDNMonitor(db, make_settings(workdir, axisnow_api_token="test-token"))
        await refresh(monitor, [row(uuid="unlimited", mode="unlimited")])
        await monitor.charge(100_000_000)
        assert not monitor.snapshot()["blocked"]
        assert monitor.snapshot()["subscriptions"][0]["quota_bytes"] is None
        db.close()

    asyncio.run(run())


def test_idle_usage_timestamp_does_not_lock_out_a_fresh_api_read(workdir):
    async def run():
        db = Database(workdir / "cdn.db")
        monitor = CDNMonitor(db, make_settings(workdir, axisnow_api_token="test-token"))
        data = row()
        data["usage_updated_at"] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        await refresh(monitor, [data])
        await monitor.check(1024)
        assert not monitor.snapshot()["blocked"]
        db.close()

    asyncio.run(run())


def test_running_stream_stops_at_budget_and_closes_telegram_reader(workdir):
    from app.api.routes.files import iter_item_range

    async def run():
        db = Database(workdir / "cdn.db")
        monitor = CDNMonitor(db, make_settings(workdir, axisnow_api_token="test-token"))
        ceiling = 1024 * 1024 * 95 // 100
        await refresh(monitor, [row(ceiling - 5)])
        closed = False

        class Storage:
            async def iter_download(self, *args):
                nonlocal closed
                try:
                    yield b"abc"
                    yield b"def"
                finally:
                    closed = True

        parts = [{"tg_message_id": 1, "size": 6}]
        stream = iter_item_range(db, Storage(), {"id": "file"}, 0, 6, parts, monitor)
        assert await anext(stream) == b"abc"
        with pytest.raises(HTTPException):
            await anext(stream)
        assert closed
        assert monitor.snapshot()["subscriptions"][0]["used"] == ceiling - 2
        db.close()

    asyncio.run(run())


def test_subscription_selection_fails_closed_and_new_month_resets_floor(workdir):
    async def run():
        db = Database(workdir / "cdn.db")
        monitor = CDNMonitor(
            db,
            make_settings(
                workdir,
                axisnow_api_token="test-token",
                axisnow_subscription_ids="limited",
            ),
        )
        db.cdn_observe([("limited", "2000-01", 9_000_000)])
        await refresh(monitor, [row(), row(9_000_000, "unrelated")])
        assert not monitor.snapshot()["blocked"]
        assert len(monitor.snapshot()["subscriptions"]) == 1
        await refresh(monitor, [row(uuid="unrelated")])
        monitor.updated_at = datetime.now(UTC) - timedelta(minutes=4)
        with pytest.raises(HTTPException):
            await monitor.check()
        db.close()

    asyncio.run(run())


def test_charge_is_atomic_across_connections(workdir):
    path = workdir / "cdn.db"
    first, second = Database(path), Database(path)
    first.cdn_observe([("a", "2026-11", 0), ("b", "2026-11", 0)])
    limits = [("a", "2026-11", 100), ("b", "2026-11", 100)]
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda db: db.cdn_charge(limits, 70), [first, second]))
    assert sorted(results) == [False, True]
    assert set(first.cdn_totals().values()) == {70}
    assert not first.cdn_charge(limits, 31)
    assert set(first.cdn_totals().values()) == {70}
    first.close()
    second.close()


def test_download_share_and_head_are_blocked_but_control_remains_available(authenticated_client):
    client, app, storage = authenticated_client
    settings = make_settings(app.state.settings.data_dir.parent, axisnow_api_token="test-token")
    monitor = CDNMonitor(app.state.traffic.db, settings)
    asyncio.run(refresh(monitor, [row(1_000_000)]))
    app.state.cdn = app.state.traffic.cdn = monitor
    item = app.state.db.create_file(
        "video.mp4",
        None,
        6,
        "video/mp4",
        None,
        str(storage.chat),
        [{"part_index": 0, "tg_message_id": 201, "size": 6}],
    )
    storage.files[201] = b"abcdef"
    url = f"/api/files/{item['id']}/download"
    assert client.head(url).status_code == 429
    assert client.get(url, headers={"Range": "bytes=0-2"}).status_code == 429
    share = client.post(f"/api/items/{item['id']}/share", json={}).json()
    assert client.get(f"/s/{share['token']}/download").status_code == 429
    assert client.get("/api/traffic").json()["cdn"]["blocked"]
    assert client.get("/api/items").status_code == 200
    assert client.get("/api/health/live").status_code == 200


def test_filter_pagination_and_credentials_stay_on_api_host():
    import json
    from io import BytesIO

    requests = []

    class Opener:
        def open(self, request, **kwargs):
            requests.append(request)
            return BytesIO(
                json.dumps(
                    {
                        "success": True,
                        "result_info": {"total_count": 2},
                        "result": [{"uuid": str(len(requests))}],
                    }
                ).encode()
            )

    with patch("urllib.request.build_opener", return_value=Opener()):
        assert len(subscription_rows("fake-token")) == 2
    assert all(
        r.full_url == "https://api.axisnow.io/client/v1/tenant_subscriptions/filter"
        for r in requests
    )
    assert all(r.get_header("Authorization") == "Bearer fake-token" for r in requests)
    assert json.loads(requests[1].data)["page"] == 2
