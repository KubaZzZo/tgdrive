from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from conftest import make_settings
from test_channel_sync import ChannelStorage, message

from app.infrastructure.db import Database
from app.services.channel_sync import ChannelSyncService


def album_message(mid, group, name, caption=""):
    msg = message(mid, name=name, caption=caption)
    msg.grouped_id = group
    if name.endswith(".jpg"):
        msg.document.mime_type = "image/jpeg"
    return msg


class AlbumStorage(ChannelStorage):
    async def get_album_messages(self, msg):
        return [m for m in self.history if m.grouped_id == msg.grouped_id]


def test_album_cover_caption_names_video_during_history_and_backfill(workdir):
    async def run():
        db = Database(workdir / "albums.sqlite3")
        storage = AlbumStorage()
        cover = album_message(13, 100, "cover.jpg", "📱旅行日记\n\n#旅行 #风景")
        video = album_message(14, 100, "222.mp4")
        unrelated = album_message(12, 99, "other.jpg", "不属于这个相册")
        storage.history = [unrelated, cover, video]
        service = ChannelSyncService(db, storage, make_settings(workdir))
        # Reproduce the filename previously imported with an empty video caption.
        service.ingest(video)
        await service.scan(True)
        item = next(i for i in db.list_items(None) if i["category"] == "video")
        assert item["name"] == "📱旅行日记.mp4"
        assert db.get_parts(item["id"])[0]["tg_message_id"] == 14
        await service.scan(True)
        assert db.get_item(item["id"])["name"] == "📱旅行日记.mp4"
        db.close()

    asyncio.run(run())


def test_live_album_caption_arriving_after_video_and_edits(workdir):
    async def run():
        db = Database(workdir / "live-albums.sqlite3")
        storage = AlbumStorage()
        video = album_message(14, 100, "222.mp4")
        storage.history = [video]
        service = ChannelSyncService(db, storage, make_settings(workdir))
        await service.on_message(video)
        item_id = db.list_items(None)[0]["id"]
        cover = album_message(13, 100, "cover.jpg", "相册简介")
        storage.history.append(cover)
        await service.on_message(cover)
        assert db.get_item(item_id)["name"] == "相册简介.mp4"
        cover.message = "修改后的相册简介"
        cover.edit_date = datetime.now(UTC) + timedelta(seconds=1)
        await service.on_edit(cover)
        assert db.get_item(item_id)["name"] == "修改后的相册简介.mp4"
        cover.message = ""
        cover.edit_date += timedelta(seconds=1)
        await service.on_edit(cover)
        assert db.get_item(item_id)["name"] == "222.mp4"
        db.rename_item(item_id, "黑丝.mp4")
        cover.message = "又修改了简介"
        cover.edit_date += timedelta(seconds=1)
        await service.on_edit(cover)
        await service.scan(True)
        assert db.get_item(item_id)["name"] == "黑丝.mp4"
        db.close()

    asyncio.run(run())


def test_own_video_caption_wins_and_ambiguous_album_is_not_guessed(workdir):
    async def run():
        db = Database(workdir / "own-caption.sqlite3")
        storage = AlbumStorage()
        storage.history = [
            album_message(1, 100, "cover.jpg", "相册说明"),
            album_message(2, 100, "video.mp4", "视频专属标题"),
            album_message(3, 100, "other.mp4"),
        ]
        service = ChannelSyncService(db, storage, make_settings(workdir))
        await service.scan(True)
        names = {item["name"] for item in db.list_items(None)}
        assert "视频专属标题.mp4" in names
        assert "other.mp4" in names  # Two conflicting captions; no shared title.
        db.close()

    asyncio.run(run())


def test_album_history_uses_fresh_member_with_its_matching_version(workdir):
    async def run():
        db = Database(workdir / "stale-album.sqlite3")
        cover = album_message(1, 100, "cover.jpg")
        old = album_message(2, 100, "video.mp4", "旧简介")
        fresh = album_message(2, 100, "video.mp4", "新简介")
        fresh.edit_date = old.date + timedelta(seconds=10)

        class FreshStorage(AlbumStorage):
            async def get_album_messages(self, msg):
                return [cover, fresh]

        storage = FreshStorage()
        storage.history = [cover, old]
        service = ChannelSyncService(db, storage, make_settings(workdir))
        await service.scan(True)
        assert any(i["name"] == "新简介.mp4" for i in db.list_items(None))
        assert not service.ingest(old)
        db.close()

    asyncio.run(run())


def test_newer_history_member_replaces_stale_album_lookup(workdir):
    async def run():
        db = Database(workdir / "newer-history.sqlite3")
        cover = album_message(1, 100, "cover.jpg", "相册说明")
        old = album_message(2, 100, "video.mp4", "旧标题")
        fresh = album_message(2, 100, "video.mp4", "最新标题")
        fresh.edit_date = old.date + timedelta(seconds=10)

        class StaleLookup(AlbumStorage):
            async def get_album_messages(self, msg):
                return [cover, old]

        storage = StaleLookup()
        storage.history = [cover, fresh]
        service = ChannelSyncService(db, storage, make_settings(workdir))
        await service.scan(True)
        assert any(i["name"] == "最新标题.mp4" for i in db.list_items(None))
        db.close()

    asyncio.run(run())


def test_deleted_edited_cover_removes_shared_title_after_rescan(workdir):
    async def run():
        db = Database(workdir / "deleted-cover.sqlite3")
        cover = album_message(1, 100, "cover.jpg", "相册说明")
        video = album_message(2, 100, "222.mp4")
        cover.edit_date = video.date + timedelta(seconds=100)
        storage = AlbumStorage()
        storage.history = [cover, video]
        service = ChannelSyncService(db, storage, make_settings(workdir))
        await service.scan(True)
        item_id = next(i["id"] for i in db.list_items(None) if i["category"] == "video")
        assert db.get_item(item_id)["name"] == "相册说明.mp4"
        storage.history = [video]
        await service.on_delete([cover.id])
        await service.scan(True)
        assert db.get_item(item_id)["name"] == "222.mp4"
        db.close()

    asyncio.run(run())


def test_album_lookup_filters_neighboring_messages_and_keeps_request_bounded():
    from app.infrastructure.storage import TelegramStorage

    async def run():
        target = album_message(14, 100, "222.mp4")
        cover = album_message(13, 100, "cover.jpg", "相册简介")
        other = album_message(12, 99, "other.jpg", "不同相册")

        class Client:
            async def get_messages(self, chat, ids):
                assert 13 in ids and 14 in ids and len(ids) <= 33
                return [None, other, cover, target]

        storage = TelegramStorage()
        storage.require_client = lambda: Client()
        assert await storage.get_album_messages(target) == [cover, target]

    asyncio.run(run())
