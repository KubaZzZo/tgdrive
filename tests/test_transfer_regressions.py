from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import FakeTelegramStorage, make_settings
from telethon import TelegramClient
from telethon.sessions import MemorySession
from telethon.tl.types import InputDocumentFileLocation

from app.api.routes.uploads import upload_chunk
from app.auth import check_password
from app.db import Database
from app.services.uploads import UploadService
from app.storage import TelegramStorage


def test_unicode_password():
    assert check_password("密码🔐", "密码🔐")
    assert not check_password("错误密码", "密码🔐")


@pytest.mark.parametrize(
    "offset,length",
    [
        (2, 3),
        (0, 1),
        (7, 3),
        (17, 800_000),
        (0, 1_200_000),
        (0, 93_470),
        (777, 24_576),
        (688_128, 360_448),
        (688_128, 524_288),
        (1_044_480, 8192),
        (4096, 524_288),
    ],
)
def test_download_uses_real_telethon_iterator(workdir, offset, length):
    async def run():
        data = (b"0123456789" * 400_000) if length > 10 else b"0123456789"
        client = TelegramClient(MemorySession(), 12345, "test")
        location = InputDocumentFileLocation(1, 1, b"", "")

        async def call(sender, request):
            assert request.offset % 4096 == 0
            assert request.limit % 4096 == 0
            assert 1048576 % request.limit == 0
            assert request.offset // 1048576 == (request.offset + request.limit - 1) // 1048576
            return SimpleNamespace(bytes=data[request.offset : request.offset + request.limit])

        client._call = call
        original = client.iter_download
        client.iter_download = lambda message, **kwargs: original(location, **kwargs)
        storage = TelegramStorage(make_settings(workdir))
        storage.client, storage.chat, storage._started = client, -100123, True
        storage._get_message = AsyncMock(return_value=SimpleNamespace(document=True))
        result = b"".join(
            [bytes(chunk) async for chunk in storage.iter_download(1, offset, length)]
        )
        assert result == data[offset : offset + length]

    asyncio.run(run())


def test_canceled_upload_removes_sent_segments(workdir):
    async def run():
        path = workdir / "data.bin"
        path.write_bytes(b"abcdefgh")
        second_segment = asyncio.Event()

        async def upload(*args, **kwargs):
            if client.upload_file.await_count == 2:
                second_segment.set()
                await asyncio.Event().wait()
            return object()

        client = SimpleNamespace(
            upload_file=AsyncMock(side_effect=upload),
            send_file=AsyncMock(return_value=SimpleNamespace(id=101)),
            delete_messages=AsyncMock(),
        )
        storage = TelegramStorage(make_settings(workdir, tg_segment_size=4))
        storage.client, storage.chat, storage._started = client, -100123, True
        task = asyncio.create_task(storage.upload_file(path, "data.bin"))
        await asyncio.wait_for(second_segment.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        client.delete_messages.assert_awaited_once_with(-100123, [101])

    asyncio.run(run())


def test_concurrent_chunks_do_not_overwrite_or_regress(workdir):
    async def run():
        path = workdir / "data.part"
        path.touch()
        db = Database(workdir / "concurrent.sqlite3")
        service = UploadService(db, FakeTelegramStorage())
        record = db.create_upload(None, "data.bin", 8, None, str(path), 8)
        entered, release = asyncio.Event(), asyncio.Event()

        class Request:
            def __init__(self, data, pause=False):
                self.headers = {"upload-offset": "0", "content-length": str(len(data))}
                self.data, self.pause = data, pause

            async def stream(self):
                if self.pause:
                    entered.set()
                    await release.wait()
                yield self.data

        first = asyncio.create_task(
            upload_chunk(record["id"], Request(b"AAAAAAAA", True), db, service)
        )
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(upload_chunk(record["id"], Request(b"BBBB"), db, service))
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, second)
        assert results[1].get("duplicate") is True
        assert db.get_upload(record["id"])["received"] == 8
        assert path.read_bytes() == b"AAAAAAAA"
        db.close()

    asyncio.run(run())


def test_cancel_waits_for_in_flight_chunk(workdir):
    from app.api.routes.uploads import cancel_upload

    async def run():
        db = Database(workdir / "cancel.sqlite3")
        service = UploadService(db, FakeTelegramStorage())
        path = workdir / "cancel.part"
        path.touch()
        record = db.create_upload(None, "a.bin", 4, None, str(path), 4)
        entered, release = asyncio.Event(), asyncio.Event()

        class Request:
            headers = {"upload-offset": "0", "content-length": "4"}

            async def stream(self):
                entered.set()
                await release.wait()
                yield b"data"

        write = asyncio.create_task(upload_chunk(record["id"], Request(), db, service))
        await asyncio.wait_for(entered.wait(), 2)
        cancel = asyncio.create_task(cancel_upload(record["id"], db, service))
        await asyncio.sleep(0)
        assert not cancel.done()
        release.set()
        await asyncio.gather(write, cancel)
        assert not path.exists()
        assert db.get_upload(record["id"]) is None
        db.close()

    asyncio.run(run())


def test_backend_concurrency_and_shutdown_recovery(workdir):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        active = peak = 0

        class SlowStorage(FakeTelegramStorage):
            async def upload_file(self, *args, **kwargs):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                entered.set()
                try:
                    await release.wait()
                    return await super().upload_file(*args, **kwargs)
                finally:
                    active -= 1

        db = Database(workdir / "queue.sqlite3")
        service = UploadService(db, SlowStorage(), concurrency=1)
        ids = []
        for i in range(3):
            path = workdir / f"queue{i}.part"
            path.write_bytes(b"data")
            record = db.create_upload(None, f"data{i}.bin", 4, None, str(path), 4)
            db.update_upload(record["id"], received=4, state="processing")
            ids.append(record["id"])
            assert service.schedule(record["id"])
        await asyncio.wait_for(entered.wait(), 2)
        assert peak == 1
        await service.shutdown()
        assert all(db.get_upload(uid)["state"] == "error" for uid in ids)
        assert not service.schedule(ids[0])
        db.close()

    asyncio.run(run())


def test_metadata_failure_removes_uploaded_messages(workdir):
    async def run():
        db = Database(workdir / "metadata.sqlite3")
        folder = db.create_folder("target", None)

        class DeletingStorage(FakeTelegramStorage):
            async def upload_file(self, *args, **kwargs):
                parts = await super().upload_file(*args, **kwargs)
                db.delete_tree(folder["id"])
                return parts

        storage = DeletingStorage()
        service = UploadService(db, storage)
        path = workdir / "metadata.part"
        path.write_bytes(b"data")
        upload = db.create_upload(folder["id"], "a.bin", 4, None, str(path), 4)
        await service._process(upload["id"])
        assert db.get_upload(upload["id"])["state"] == "error"
        assert storage.deleted_message_ids == [101]
        assert not storage.files
        assert path.exists()
        db.close()

    asyncio.run(run())


def test_stale_cleanup_rechecks_after_waiting_for_writer(workdir):
    from datetime import UTC, datetime, timedelta

    async def run():
        db = Database(workdir / "cleanup.sqlite3")
        service = UploadService(db, FakeTelegramStorage(), stale_hours=1)
        path = workdir / "cleanup.part"
        path.write_bytes(b"data")
        upload = db.create_upload(None, "a.bin", 4, None, str(path), 4)
        old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        with db.conn:
            db.conn.execute("UPDATE uploads SET updated_at=? WHERE id=?", (old, upload["id"]))
        async with service.lock(upload["id"]):
            cleanup = asyncio.create_task(service.cleanup_stale_async())
            await asyncio.sleep(0)
            db.update_upload(upload["id"], received=4)
        await cleanup
        assert path.exists()
        assert db.get_upload(upload["id"]) is not None
        db.close()

    asyncio.run(run())


def test_download_closes_stream_on_early_disconnect(workdir):
    async def run():
        class Stream:
            closed = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                return b"data"

            async def close(self):
                self.closed = True

        stream = Stream()
        storage = TelegramStorage(make_settings(workdir))
        storage.client = SimpleNamespace(iter_download=lambda *args, **kwargs: stream)
        storage.chat, storage._started = -100123, True
        storage._get_message = AsyncMock(return_value=SimpleNamespace(document=True))
        download = storage.iter_download(1, 0, 100)
        assert await anext(download) == b"data"
        await download.aclose()
        assert stream.closed

    asyncio.run(run())


def test_unicode_password_login_preserves_whitespace(workdir):
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(make_settings(workdir, app_password=" 密码🔐 "), storage=FakeTelegramStorage())
    with TestClient(app) as client:
        assert client.post("/api/login", json={"password": "密码🔐"}).status_code == 401
        assert client.post("/api/login", json={"password": " 密码🔐 "}).status_code == 200


def test_message_cache_is_bounded_and_reuses_hits(workdir):
    async def run():
        storage = TelegramStorage(make_settings(workdir))
        client = SimpleNamespace(
            get_messages=AsyncMock(return_value=SimpleNamespace(document=True))
        )
        storage.client, storage.chat, storage._started = client, -100123, True
        for mid in range(300):
            await storage._get_message(mid)
        await storage._get_message(299)
        assert client.get_messages.await_count == 300
        assert len(storage._message_cache) == 256
        await storage._get_message(0)
        assert client.get_messages.await_count == 301
        assert len(storage._message_cache) == 256

    asyncio.run(run())


def test_download_closes_under_anyio_disconnect_cancellation(workdir):
    import anyio

    async def run():
        class Stream:
            closed = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                return b"data"

            async def close(self):
                await anyio.sleep(0)
                self.closed = True

        stream = Stream()
        storage = TelegramStorage(make_settings(workdir))
        storage.client = SimpleNamespace(iter_download=lambda *args, **kwargs: stream)
        storage.chat, storage._started = -100123, True
        storage._get_message = AsyncMock(return_value=SimpleNamespace(document=True))
        download = storage.iter_download(1, 0, 100)
        await anext(download)
        with anyio.CancelScope() as scope:
            scope.cancel()
            await download.aclose()
        assert stream.closed

    asyncio.run(run())
