from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from conftest import FakeTelegramStorage, make_settings
from telethon.tl import types

from app.db import Database
from app.services.channel_sync import ChannelSyncService, describe_message


def message(mid, name="phone.mp4", size=20, caption="", media_id=None):
    return types.Message(
        id=mid,
        peer_id=types.PeerChannel(123),
        date=datetime.now(UTC),
        message=caption,
        media=types.MessageMediaDocument(
            document=types.Document(
                id=media_id or 1000 + mid,
                access_hash=0,
                file_reference=b"",
                date=datetime.now(UTC),
                mime_type="video/mp4",
                size=size,
                dc_id=1,
                attributes=[types.DocumentAttributeFilename(name)],
            )
        ),
    )


class ChannelStorage(FakeTelegramStorage):
    def __init__(self):
        super().__init__()
        self.started = True
        self.history = []
        self.invalidated = []
        self.detached = False
        self.fail_after = None

    async def subscribe_channel(self, new, edit, delete):
        self.new, self.edit, self.delete = new, edit, delete
        return lambda: setattr(self, "detached", True)

    async def latest_channel_message_id(self):
        return max((msg.id for msg in self.history), default=0)

    async def iter_channel_messages(self, after_id, max_id):
        for msg in sorted(self.history, key=lambda msg: msg.id):
            if self.fail_after == msg.id:
                raise ConnectionError("interrupted scan")
            if after_id < msg.id <= max_id:
                yield msg

    def invalidate_message(self, mid):
        self.invalidated.append(mid)


def test_real_telegram_photo_descriptor_and_sanitized_names():
    now = datetime.now(UTC)
    photo = types.Photo(
        id=10,
        access_hash=0,
        file_reference=b"",
        date=now,
        sizes=[
            types.PhotoSize(type="m", w=10, h=10, size=50),
            types.PhotoSizeProgressive(type="x", w=20, h=20, sizes=[80, 100]),
        ],
        dc_id=1,
    )
    msg = types.Message(
        id=2,
        peer_id=types.PeerChannel(123),
        date=now,
        message="",
        media=types.MessageMediaPhoto(photo=photo),
    )
    result = describe_message(msg)
    assert result["size"] == 100 and result["mime"] == "image/jpeg"
    assert result["name"].endswith(".jpg")
    assert describe_message(message(3, "../unsafe/name.mp4"))["name"] == "_unsafe_name.mp4"
    assert describe_message(message(4, caption="tgdrive:v1:abc:0")) is None
    assert (
        describe_message(
            types.Message(id=5, peer_id=types.PeerChannel(123), date=now, message="plain text")
        )
        is None
    )


def test_history_import_idempotence_restart_and_deletion(workdir):
    async def run():
        path = workdir / "sync.sqlite3"
        db = Database(path)
        storage = ChannelStorage()
        storage.history = [
            message(1, "photo.jpg"),
            message(2),
            message(3, caption="tgdrive:v1:web:0"),
        ]
        service = ChannelSyncService(db, storage, make_settings(workdir))
        await service.scan(True)
        assert db.channel_file_count(str(storage.chat)) == 2
        assert db.channel_cursor(str(storage.chat)) == 3
        await service.scan(True)
        assert db.channel_file_count(str(storage.chat)) == 2
        db.close()
        db = Database(path)
        service = ChannelSyncService(db, storage, make_settings(workdir))
        storage.history.append(message(4, "audio.mp3"))
        await service.scan(False)
        assert db.channel_file_count(str(storage.chat)) == 3
        # Simulate removal while the application was offline; full scan reconciles even an empty channel.
        storage.history = []
        await service.scan(True)
        assert db.channel_file_count(str(storage.chat)) == 0
        db.close()

    asyncio.run(run())


def test_live_edit_delete_and_local_deletion_tombstone(workdir):
    async def run():
        db = Database(workdir / "events.sqlite3")
        storage = ChannelStorage()
        service = ChannelSyncService(db, storage, make_settings(workdir))
        await service.on_message(message(1))
        item = db.list_items(None)[0]
        db.rename_item(item["id"], "custom.mp4")
        await service.on_edit(message(1, caption="new caption"))
        assert db.get_item(item["id"])["name"] == "custom.mp4"
        await service.on_edit(message(1, "replacement.mp3", size=42, media_id=99))
        updated = db.get_item(item["id"])
        assert updated["name"] == "replacement.mp3" and updated["category"] == "audio"
        assert db.get_parts(item["id"])[0]["size"] == 42
        await service.on_delete([1])
        assert db.get_item(item["id"]) is None
        await service.on_message(message(1))
        assert db.channel_file_count(str(storage.chat)) == 0
        await service.on_message(message(2))
        local = db.list_items(None)[0]
        db.delete_tree(local["id"])
        await service.on_message(message(2))
        assert db.channel_file_count(str(storage.chat)) == 0
        assert set(storage.invalidated) == {1, 2}
        db.close()

    asyncio.run(run())


def test_failed_scan_preserves_existing_items_and_retries_after_cursor(workdir):
    async def run():
        db = Database(workdir / "failure.sqlite3")
        storage = ChannelStorage()
        storage.history = [message(1), message(2)]
        service = ChannelSyncService(db, storage, make_settings(workdir))
        service.ingest(message(10))
        storage.fail_after = 2
        with pytest.raises(ConnectionError):
            await service.scan(True)
        assert (
            db.channel_file_count(str(storage.chat)) == 2
        )  # Existing id=10 survives incomplete snapshot.
        assert db.channel_cursor(str(storage.chat)) == 1
        storage.fail_after = None
        await service.scan(False)
        assert db.channel_file_count(str(storage.chat)) == 3
        await service.scan(True)
        assert db.channel_file_count(str(storage.chat)) == 2
        db.close()

    asyncio.run(run())


def test_existing_web_upload_not_duplicated_and_late_live_message_survives_snapshot(workdir):
    async def run():
        db = Database(workdir / "race.sqlite3")
        storage = ChannelStorage()
        db.create_file(
            "web.mp4",
            None,
            20,
            "video/mp4",
            None,
            str(storage.chat),
            [{"part_index": 0, "tg_message_id": 1, "size": 20}],
        )
        service = ChannelSyncService(db, storage, make_settings(workdir))
        await service.on_message(message(1))
        assert len(db.list_items(None)) == 1
        storage.history = [message(2)]
        original = storage.iter_channel_messages

        async def race(after_id, max_id):
            async for msg in original(after_id, max_id):
                yield msg
            await service.on_message(message(3))

        storage.iter_channel_messages = race
        await service.scan(True)
        assert db.channel_file_count(str(storage.chat)) == 2
        db.close()

    asyncio.run(run())


def test_worker_starts_and_detaches_on_shutdown(workdir):
    async def run():
        db = Database(workdir / "worker.sqlite3")
        storage = ChannelStorage()
        storage.history = [message(1)]
        service = ChannelSyncService(db, storage, make_settings(workdir))
        await service.start()

        async def imported():
            while db.channel_file_count(str(storage.chat)) == 0:
                await asyncio.sleep(0.001)

        await asyncio.wait_for(imported(), 2)
        assert service.status()["files"] == 1
        await service.shutdown()
        assert storage.detached and service._worker.done()
        db.close()

    asyncio.run(run())


def test_sync_api_is_authenticated_and_head_does_not_download(app_client):
    client, app, storage = app_client
    assert client.get("/api/sync").status_code == 401
    assert client.post("/api/sync", json={"full": True}).status_code == 401
    client.post("/api/login", json={"password": "correct-horse-battery-staple"})
    assert client.get("/api/sync").status_code == 200
    assert client.post("/api/sync", json={"full": True}).status_code == 503
    item = app.state.db.create_file(
        "movie.mp4",
        None,
        100,
        "video/mp4",
        None,
        str(storage.chat),
        [{"part_index": 0, "tg_message_id": 999, "size": 100}],
    )
    response = client.head(f"/api/files/{item['id']}/download", headers={"Range": "bytes=10-19"})
    assert response.status_code == 206 and response.content == b""
    assert response.headers["content-length"] == "10"


def test_stale_history_cannot_overwrite_newer_edit(workdir):
    from datetime import timedelta

    async def run():
        db = Database(workdir / "stale-edit.sqlite3")
        storage = ChannelStorage()
        service = ChannelSyncService(db, storage, make_settings(workdir))
        old = message(1, "old.mp4", media_id=10)
        newer = message(1, "new.mp3", media_id=11)
        newer.edit_date = old.date + timedelta(seconds=20)
        await service.on_message(old)
        await service.on_edit(newer)
        service.ingest(old, "scan-id")
        assert db.list_items(None)[0]["name"] == "new.mp3"
        db.close()

    asyncio.run(run())


def test_full_rescan_requested_during_scan_is_not_lost(workdir):
    async def run():
        entered, release, second = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class BlockingStorage(ChannelStorage):
            calls = 0

            async def latest_channel_message_id(self):
                self.calls += 1
                if self.calls == 1:
                    entered.set()
                    await release.wait()
                if self.calls == 2:
                    second.set()
                return 0

        db = Database(workdir / "pending.sqlite3")
        storage = BlockingStorage()
        service = ChannelSyncService(db, storage, make_settings(workdir))
        await service.start()
        await asyncio.wait_for(entered.wait(), 2)
        assert service.request_scan(True)
        release.set()
        await asyncio.wait_for(second.wait(), 2)
        await service.shutdown()
        assert not service.request_scan(True)
        db.close()

    asyncio.run(run())
