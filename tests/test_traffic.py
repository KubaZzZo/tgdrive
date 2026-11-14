from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.infrastructure.db import Database
from app.services.traffic import TrafficMiddleware, TrafficMonitor
from scripts.traffic_collector import read_interfaces, sample


@pytest.mark.skipif(sys.platform != "linux", reason="Linux host counters")
def test_linux_uplink_counters_match_sysfs():
    names = list(read_interfaces())
    before = {
        name: [
            int((Path("/sys/class/net") / name / "statistics" / field).read_text())
            for field in ("rx_bytes", "tx_bytes")
        ]
        for name in names
    }
    measured = read_interfaces(selected=names)
    for name in names:
        after = [
            int((Path("/sys/class/net") / name / "statistics" / field).read_text())
            for field in ("rx_bytes", "tx_bytes")
        ]
        assert all(before[name][i] <= measured[name][i] <= after[i] for i in (0, 1))


def instant(value="2026-11-04T04:00:00+00:00"):
    return datetime.fromisoformat(value)


def test_collector_baseline_restart_and_interface_filter(workdir):
    proc = workdir / "proc"
    (proc / "net").mkdir(parents=True)
    (proc / "net/dev").write_text(
        "header\nheader\nlo: 900 0 0 0 0 0 0 0 900\neth0: 100 0 0 0 0 0 0 0 200\nveth0: 800 0 0 0 0 0 0 0 800\n"
    )
    (proc / "net/route").write_text("header\neth0 00000000 00000000 0003\n")
    assert read_interfaces(proc) == {"eth0": [100, 200]}
    with pytest.raises(RuntimeError):
        read_interfaces(proc, ["lo"])
    first = sample(None, {"eth0": [100, 200]}, "boot1", instant())
    assert first["days"]["2026-11-04"] == {"inbound": 0, "outbound": 0}
    # JSON reload simulates restarting the collector without resetting accumulated usage.
    second = sample(
        json.loads(json.dumps(first)),
        {"eth0": [150, 280]},
        "boot1",
        instant() + timedelta(seconds=5),
    )
    assert second["days"]["2026-11-04"] == {"inbound": 50, "outbound": 80}
    assert first["days"]["2026-11-04"]["inbound"] == 0


def test_collector_beijing_midnight_reset_and_reboot():
    first = sample(None, {"eth0": [10, 20]}, "a", instant("2026-11-04T15:59:58+00:00"))
    second = sample(first, {"eth0": [15, 30]}, "a", instant("2026-11-04T16:00:03+00:00"))
    assert second["days"]["2026-11-05"] == {"inbound": 5, "outbound": 10}
    assert "2026-11-05" not in second["incomplete_days"]
    rebooted = sample(second, {"eth0": [2, 4]}, "b", instant("2026-11-04T16:00:08+00:00"))
    assert rebooted["days"]["2026-11-05"] == {"inbound": 7, "outbound": 14}
    assert "2026-11-05" in rebooted["incomplete_days"]


def test_cross_day_gap_and_new_interface_mark_incomplete():
    first = sample(None, {"eth0": [10, 20]}, "a", instant())
    second = sample(
        first, {"eth0": [100, 200], "eth1": [999, 999]}, "a", instant() + timedelta(days=1)
    )
    assert second["gaps"] == 1
    assert "2026-11-05" in second["incomplete_days"]
    assert second["days"]["2026-11-05"] == {"inbound": 90, "outbound": 180}


def test_monitor_persistence_quota_and_stale_host_state(workdir):
    db = Database(workdir / "traffic.db")
    host_path = workdir / "host.json"
    monitor = TrafficMonitor(db, host_path)
    monitor.record("web", inbound=123, outbound=456)
    monitor.flush()
    assert db.traffic_rows("2000")[0]["outbound"] == 456
    state = sample(None, {"eth0": [0, 0]}, "a", instant())
    state = sample(
        state, {"eth0": [40_000_000_000, 45_000_000_000]}, "a", instant() + timedelta(seconds=5)
    )
    host_path.write_text(json.dumps(state))
    status = monitor.snapshot(instant() + timedelta(seconds=6))
    assert status["host"]["used"] == 85_000_000_000
    assert status["host"]["warning"] == "near"
    assert status["host"]["remaining"] == 15_000_000_000
    db.update_traffic_config(80_000_000_000)
    assert monitor.snapshot(instant() + timedelta(seconds=6))["host"]["warning"] == "exceeded"
    assert monitor.snapshot(instant() + timedelta(minutes=2))["host"]["stale"]
    db.close()
    reopened = Database(workdir / "traffic.db")
    assert reopened.traffic_config()["quota_bytes"] == 80_000_000_000
    assert reopened.traffic_rows("2000")[0]["inbound"] == 123
    reopened.close()


def test_monitor_missing_corrupt_and_failed_persistence(workdir, monkeypatch):
    db = Database(workdir / "bad.db")
    path = workdir / "bad.json"
    monitor = TrafficMonitor(db, path)
    assert not monitor.snapshot()["host"]["available"]
    path.write_text("null")
    assert not monitor.snapshot()["host"]["available"]
    with monkeypatch.context() as patch:
        patch.setattr(
            db, "record_traffic", lambda rows: (_ for _ in ()).throw(OSError("disk full"))
        )
        monitor.record("web", inbound=1024 * 1024)
    monitor.flush()
    assert db.traffic_rows("2000")[0]["inbound"] == 1024 * 1024
    db.close()


def test_traffic_api_authentication_and_quota(app_client):
    client, app, _ = app_client
    assert client.get("/api/traffic").status_code == 401
    assert client.put("/api/traffic/settings", json={"quota_bytes": 1}).status_code == 401
    client.post("/api/login", json={"password": "correct-horse-battery-staple"})
    result = client.get("/api/traffic").json()
    assert result["quota_bytes"] == 100_000_000_000
    assert result["timezone"] == "Asia/Shanghai"
    assert client.put("/api/traffic/settings", json={"quota_bytes": 0}).status_code == 422
    assert (
        client.put("/api/traffic/settings", json={"quota_bytes": 90_000_000_000}).json()[
            "quota_bytes"
        ]
        == 90_000_000_000
    )


def test_streaming_middleware_counts_actual_bodies_including_partial_response(workdir):
    async def run():
        db = Database(workdir / "stream.db")
        monitor = TrafficMonitor(db, workdir / "missing.json")

        async def application(scope, receive, send):
            await receive()
            await send({"type": "http.response.start", "status": 206, "headers": []})
            await send({"type": "http.response.body", "body": b"abc", "more_body": True})
            raise asyncio.CancelledError

        async def receive():
            return {"type": "http.request", "body": b"upload", "more_body": False}

        async def send(message):
            pass

        with pytest.raises(asyncio.CancelledError):
            await TrafficMiddleware(application, monitor)({"type": "http"}, receive, send)
        monitor.flush()
        row = db.traffic_rows("2000")[0]
        assert (row["inbound"], row["outbound"]) == (6, 3)
        db.close()

    asyncio.run(run())


def test_telegram_payload_counters(workdir):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from conftest import make_settings

    from app.infrastructure.storage import TelegramStorage

    async def run():
        db = Database(workdir / "telegram.db")
        monitor = TrafficMonitor(db, workdir / "no-host.json")
        storage = TelegramStorage(make_settings(workdir, tg_segment_size=3))
        storage.traffic = monitor
        storage.chat, storage._started = -100123, True

        async def upload(reader, **kwargs):
            await kwargs["progress_callback"](2, 3)
            await kwargs["progress_callback"](2, 3)  # Repeated progress is not counted twice.
            await kwargs["progress_callback"](3, 3)
            return object()

        class Download:
            def __init__(self):
                self.chunks = iter([b"abc", b"def"])

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self.chunks)
                except StopIteration:
                    raise StopAsyncIteration from None

            async def close(self):
                pass

        storage.client = SimpleNamespace(
            upload_file=upload,
            send_file=AsyncMock(return_value=SimpleNamespace(id=123)),
            iter_download=lambda *args, **kwargs: Download(),
        )
        path = workdir / "test.bin"
        path.write_bytes(b"abcdef")
        await storage.upload_file(path, "test.bin")
        storage._get_message = AsyncMock(return_value=SimpleNamespace(document=True))
        assert b"".join([chunk async for chunk in storage.iter_download(123, 0, 4)]) == b"abcd"
        monitor.flush()
        row = db.traffic_rows("2000")[0]
        # Count download overfetch too; only four bytes were passed to the HTTP response.
        assert (row["inbound"], row["outbound"]) == (6, 6)
        db.close()

    asyncio.run(run())
