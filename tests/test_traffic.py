from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from app.infrastructure.db import Database
from app.services.traffic import TrafficMiddleware, TrafficMonitor


def test_monitor_persistence_history_and_quota(workdir):
    db = Database(workdir / "traffic.db")
    monitor = TrafficMonitor(db)
    db.record_traffic([("2026-11-04", "web", 10, 20)])
    now = datetime.fromisoformat("2026-11-04T16:00:00+00:00")
    db.record_traffic([("2026-11-05", "telegram", 40_000_000_000, 45_000_000_000)])
    status = monitor.snapshot(now)
    assert status["day"] == "2026-11-05"
    assert status["usage"]["history"][-2] == {"day": "2026-11-04", "inbound": 10, "outbound": 20}
    assert status["usage"]["used"] == 85_000_000_000
    assert status["usage"]["warning"] == "near"
    assert status["usage"]["remaining"] == 15_000_000_000
    db.update_traffic_config(80_000_000_000)
    assert monitor.snapshot(now)["usage"]["warning"] == "exceeded"
    db.close()
    reopened = Database(workdir / "traffic.db")
    assert TrafficMonitor(reopened).snapshot(now)["quota_bytes"] == 80_000_000_000
    reopened.close()


def test_pending_bytes_visible_and_persistence_retries(workdir, monkeypatch):
    db = Database(workdir / "retry.db")
    monitor = TrafficMonitor(db)
    monitor.record("web", inbound=123, outbound=456)
    assert db.traffic_rows("2000") == []
    assert monitor.snapshot()["usage"]["used"] == 579
    with monkeypatch.context() as patch:

        def fail(rows):
            # Recording stays independent of database I/O.
            monitor.record("telegram", inbound=10)
            raise OSError("disk full")

        patch.setattr(db, "record_traffic", fail)
        monitor.flush()
    assert monitor.snapshot()["usage"]["used"] == 589
    monitor.flush()
    assert monitor.snapshot()["usage"]["used"] == 589
    assert len(db.traffic_rows("2000")) == 2
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
        monitor = TrafficMonitor(db)

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
        monitor = TrafficMonitor(db)
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
