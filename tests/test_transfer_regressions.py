from __future__ import annotations

import asyncio
import errno
import time
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import FakeTelegramStorage, make_settings
from fastapi import HTTPException
from starlette.requests import ClientDisconnect
from telethon import TelegramClient
from telethon.sessions import MemorySession
from telethon.tl.types import InputDocumentFileLocation

from app.api.routes.uploads import upload_chunk
from app.core.security import check_password
from app.infrastructure.db import Database
from app.infrastructure.storage import TelegramStorage
from app.services.uploads import UploadService


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


@pytest.mark.parametrize("reconnected", [False, True])
def test_concurrent_chunks_do_not_overwrite_or_regress(workdir, reconnected):
    async def run():
        path = workdir / "data.part"
        path.touch()
        db = Database(workdir / "concurrent.sqlite3")
        service = UploadService(db, FakeTelegramStorage())
        second_service = (
            UploadService(db, FakeTelegramStorage(), space=service.space, locks=service.locks)
            if reconnected
            else service
        )
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
        second = asyncio.create_task(
            upload_chunk(record["id"], Request(b"BBBB"), db, second_service)
        )
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, second)
        assert results[1].get("duplicate") is True
        assert db.get_upload(record["id"])["received"] == 8
        assert path.read_bytes() == b"AAAAAAAA"
        db.close()

    asyncio.run(run())


def test_chunk_preserves_confirmed_prefix_when_disk_reserve_is_reached(workdir, monkeypatch):
    async def run():
        path = workdir / "upload_disk.part"
        path.write_bytes(b"old")
        db = Database(workdir / "disk.sqlite3")
        try:
            service = UploadService(db, FakeTelegramStorage())
            record = db.create_upload(None, "data.bin", 8, None, str(path), 8)
            db.update_upload(record["id"], received=3)
            monkeypatch.setattr(
                "app.services.uploads.shutil.disk_usage",
                lambda _: SimpleNamespace(free=service.space.min_free_bytes + 4),
            )

            class Request:
                headers = {"upload-offset": "3"}

                async def stream(self):
                    yield b"abc"
                    yield b"de"

            with pytest.raises(HTTPException) as failure:
                await upload_chunk(record["id"], Request(), db, service)
            assert failure.value.status_code == 507
            assert path.read_bytes() == b"old"
            assert db.get_upload(record["id"])["received"] == 3
        finally:
            db.close()

    asyncio.run(run())


def test_repeated_cancellation_waits_for_disk_write_before_rollback(workdir, monkeypatch):
    async def run():
        path = workdir / "upload_cancel.part"
        path.write_bytes(b"old")
        db = Database(workdir / "canceled-write.sqlite3")
        service = UploadService(db, FakeTelegramStorage())
        record = db.create_upload(None, "data.bin", 6, None, str(path), 8)
        db.update_upload(record["id"], received=3)
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = Event()
        original_write = service.space._write

        def delayed_write(file, data):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(5):
                raise TimeoutError("test did not release the disk write")
            original_write(file, data)

        monkeypatch.setattr(service.space, "_write", delayed_write)

        class Request:
            headers = {"upload-offset": "3"}

            async def stream(self):
                yield b"new"

        task = asyncio.create_task(upload_chunk(record["id"], Request(), db, service))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert path.read_bytes() == b"old"
            assert db.get_upload(record["id"])["received"] == 3
            assert not service.space._reservations
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            db.close()

    asyncio.run(run())


def test_chunk_reserves_once_and_accounts_for_unwritten_bytes(workdir, monkeypatch):
    async def run():
        db = Database(workdir / "reservations.sqlite3")
        service = UploadService(db, FakeTelegramStorage())
        service.space.max_bytes = 12
        scan = service.space._check
        scans = []

        def count_scan(directory, growth):
            scans.append(growth)
            scan(directory, growth)

        monkeypatch.setattr(service.space, "_check", count_scan)
        one, two = (workdir / f"upload_{index}.part" for index in range(2))
        one.write_bytes(b"old")
        two.touch()
        first = db.create_upload(None, "first.bin", 8, None, str(one), 8)
        db.update_upload(first["id"], received=3)
        second = db.create_upload(None, "second.bin", 5, None, str(two), 8)
        paused, release = asyncio.Event(), asyncio.Event()

        class Request:
            headers = {"upload-offset": "3", "content-length": "5"}

            async def stream(self):
                yield b"1"
                paused.set()
                await release.wait()
                yield b"2"
                yield b"345"

        task = asyncio.create_task(upload_chunk(first["id"], Request(), db, service))
        try:
            await asyncio.wait_for(paused.wait(), 2)
            assert one.stat().st_size == 4
            with pytest.raises(HTTPException) as failure:
                await upload_chunk(
                    second["id"],
                    SimpleNamespace(headers={"upload-offset": "0", "content-length": "5"}),
                    db,
                    service,
                )
            assert failure.value.status_code == 507
            release.set()
            assert (await task)["offset"] == 8
            assert one.read_bytes() == b"old12345" and two.stat().st_size == 0
            assert scans == [5, 5]
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            db.close()

    asyncio.run(run())


def test_client_disconnect_rolls_back_only_unconfirmed_bytes(workdir):
    async def run():
        db = Database(workdir / "disconnect.sqlite3")
        service = UploadService(db, FakeTelegramStorage())
        path = workdir / "upload_disconnected.part"
        path.write_bytes(b"old")
        record = db.create_upload(None, "a.bin", 8, None, str(path), 8)
        db.update_upload(record["id"], received=3)

        class Request:
            headers = {"upload-offset": "3", "content-length": "5"}

            async def stream(self):
                yield b"new"
                raise ClientDisconnect

        try:
            with pytest.raises(HTTPException) as failure:
                await upload_chunk(record["id"], Request(), db, service)
            assert failure.value.status_code == 400
            assert path.read_bytes() == b"old"
            assert db.get_upload(record["id"])["received"] == 3

            class Retry(Request):
                async def stream(self):
                    yield b"123"
                    yield b"45"

            result = await upload_chunk(record["id"], Retry(), db, service)
            assert result["offset"] == 8
            assert path.read_bytes() == b"old12345"
        finally:
            db.close()

    asyncio.run(run())


@pytest.mark.parametrize("disk_full", [False, True])
def test_failed_patch_keeps_last_confirmed_offset(workdir, monkeypatch, disk_full):
    async def run():
        db = Database(workdir / "rollback.sqlite3")
        service = UploadService(db, FakeTelegramStorage())
        path = workdir / "upload_rollback.part"
        path.write_bytes(b"old")
        record = db.create_upload(None, "a.bin", 8, None, str(path), 8)
        db.update_upload(record["id"], received=3)

        def short_write(file, chunk):
            file.write(chunk[:1])
            raise OSError(errno.ENOSPC, "no space")

        if disk_full:
            monkeypatch.setattr(service.space, "_write", short_write)

        class Request:
            headers = {"upload-offset": "3", "content-length": "5"}

            async def stream(self):
                yield b"abc"

        try:
            with pytest.raises(HTTPException) as failure:
                await upload_chunk(record["id"], Request(), db, service)
            assert failure.value.status_code == (507 if disk_full else 400)
            assert path.read_bytes() == b"old"
            assert db.get_upload(record["id"])["received"] == 3
            assert not service.space._reservations
        finally:
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
        await storage.start()
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


@pytest.mark.parametrize("image_host", [False, True])
def test_upload_completion_failure_rolls_back_metadata_and_retries_once(workdir, image_host):
    async def run():
        db = Database(workdir / "completion.sqlite3")
        storage = FakeTelegramStorage()
        await storage.start()
        service = UploadService(db, storage)
        path = workdir / "completion.part"
        data = b"\x89PNG\r\n\x1a\nreview"
        path.write_bytes(data)
        upload = db.create_upload(
            None, "image.png", len(data), "image/png", str(path), 8, image_host=image_host
        )
        db.update_upload(upload["id"], received=len(data))
        try:
            with db.conn:
                db.conn.execute(
                    """CREATE TRIGGER fail_upload_completion BEFORE UPDATE OF state ON uploads
                       WHEN NEW.state='done' BEGIN
                           SELECT RAISE(ABORT, 'simulated completion failure');
                       END"""
                )
            await service._process(upload["id"])
            failed = db.get_upload(upload["id"])
            assert failed["state"] == "error" and failed["item_id"] is None
            assert failed["received"] == len(data)
            assert db.stats()["files"] == 0
            assert db.conn.execute("SELECT COUNT(*) FROM file_parts").fetchone()[0] == 0
            assert db.list_hosted_images("", 10, 0)[1] == 0
            assert storage.deleted_message_ids == [101] and not storage.files
            assert path.read_bytes() == data

            with db.conn:
                db.conn.execute("DROP TRIGGER fail_upload_completion")
            await service._process(upload["id"])
            completed = db.get_upload(upload["id"])
            assert completed["state"] == "done" and completed["progress"] == len(data)
            assert db.get_item(completed["item_id"])["size"] == len(data)
            assert db.get_parts(completed["item_id"])[0]["tg_message_id"] == 102
            assert db.stats()["files"] == 1
            assert db.list_hosted_images("", 10, 0)[1] == int(image_host)
            assert storage.files == {102: data}
            assert not path.exists()

            await service._process(upload["id"])
            assert db.get_upload(upload["id"])["state"] == "done"
            assert db.stats()["files"] == 1 and storage.files == {102: data}
        finally:
            db.close()

    asyncio.run(run())


@pytest.mark.parametrize("fail_at", ["send", "commit"])
def test_failed_upload_rollback_is_durable_across_retry(workdir, monkeypatch, fail_at):
    async def run():
        db = Database(workdir / "rollback.sqlite3")
        path = workdir / "data.part"
        path.write_bytes(b"abcdefgh")
        upload = db.create_upload(None, "data.bin", 8, None, str(path), 4)
        db.update_upload(upload["id"], received=8)
        storage = TelegramStorage(make_settings(workdir, tg_segment_size=4))
        chat_id = "-100123"
        storage.chat, storage._started = int(chat_id), True
        storage.client = SimpleNamespace(
            upload_file=AsyncMock(),
            send_file=AsyncMock(
                side_effect=[
                    SimpleNamespace(id=101),
                    RuntimeError("Telegram send failed")
                    if fail_at == "send"
                    else SimpleNamespace(id=102),
                    SimpleNamespace(id=103),
                    SimpleNamespace(id=104),
                ]
            ),
            delete_messages=AsyncMock(side_effect=RuntimeError("Telegram delete failed")),
        )
        service = UploadService(db, storage)
        create_file = db.create_file

        def fail_commit(**kwargs):
            assert db.has_telegram_cleanup(upload["id"])
            assert not db.due_telegram_cleanup(chat_id, time.time())
            raise RuntimeError("File commit failed")

        if fail_at == "commit":
            monkeypatch.setattr(db, "create_file", fail_commit)
        try:
            await service._process(upload["id"])
            assert db.get_upload(upload["id"])["state"] == "error"
            old_messages = [101] if fail_at == "send" else [101, 102]
            assert [
                job["message_id"] for job in db.due_telegram_cleanup(chat_id, time.time() + 60)
            ] == old_messages

            monkeypatch.setattr(db, "create_file", create_file)
            await service._process(upload["id"])
            record = db.get_upload(upload["id"])
            assert record["state"] == "done"
            assert [part["tg_message_id"] for part in db.get_parts(record["item_id"])] == [103, 104]
            with db.conn:
                db.conn.execute("UPDATE telegram_cleanup SET retry_at=0")
            storage.client.delete_messages.side_effect = None
            await service.cleanup.process()
            storage.client.delete_messages.assert_awaited_with(int(chat_id), old_messages)
            assert not db.has_telegram_cleanup(upload["id"])
        finally:
            db.close()

    asyncio.run(run())


def test_stale_cleanup_skips_active_writer(workdir):
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
            assert service.cleanup_stale() == 0
            db.update_upload(upload["id"], received=4)
        assert service.cleanup_stale() == 0
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
