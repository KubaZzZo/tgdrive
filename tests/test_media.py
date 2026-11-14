from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from conftest import make_settings
from test_channel_sync import ChannelStorage, message

from app.infrastructure.db import Database
from app.services.channel_sync import ChannelSyncService, describe_message


def test_video_caption_repairs_generic_names_but_preserves_local_rename(workdir):
    db = Database(workdir / "media.sqlite3")
    storage = ChannelStorage()
    service = ChannelSyncService(db, storage, make_settings(workdir))
    msg = message(1, "2.mp4")
    service.ingest(msg)
    item_id = db.conn.execute("SELECT id FROM items").fetchone()[0]
    msg.message = "旅途记录\nhttps://example.com"
    assert describe_message(msg)["name"] == "旅途记录.mp4"
    assert service.ingest(msg)
    assert db.get_item(item_id)["name"] == "旅途记录.mp4"
    db.conn.execute("UPDATE items SET name='我的名字.mp4' WHERE id=?", (item_id,))
    assert not service.ingest(msg)
    assert db.get_item(item_id)["name"] == "我的名字.mp4"
    assert describe_message(message(2, "holiday.mp4", caption="别的标题"))["name"] == "别的标题.mp4"
    db.close()


@pytest.mark.parametrize(
    ("caption", "expected"),
    [
        ("旅行日记\n雨中的小镇\n\n订阅频道看更多", "旅行日记 雨中的小镇.mp4"),
        ("https://example.com\n@channel\n#tag\n旅行记录", "旅行记录.mp4"),
        ("#旅行 今天的风景", "#旅行 今天的风景.mp4"),
        ("  \n\t", "holiday.mp4"),
        ("https://example.com\n#tag", "holiday.mp4"),
        ("山/水\\城市\x00夜景", "山_水_城市_夜景.mp4"),
        ("假期.MP4", "假期.mp4"),
        ("风景" * 300, ("风景" * 300)[:251] + ".mp4"),
        (".../", "_.mp4"),
    ],
)
def test_video_caption_names(caption, expected):
    assert describe_message(message(1, "holiday.mp4", caption=caption))["name"] == expected


def test_caption_edit_removal_and_explicit_rename_survive_rescan(workdir):
    db = Database(workdir / "caption.sqlite3")
    service = ChannelSyncService(db, ChannelStorage(), make_settings(workdir))
    msg = message(1, "holiday.mp4", caption="第一段标题")
    service.ingest(msg)
    item_id = db.list_items(None)[0]["id"]
    assert db.get_item(item_id)["name"] == "第一段标题.mp4"
    for caption, name in [("修改后的简介", "修改后的简介.mp4"), ("", "holiday.mp4")]:
        msg.message = caption
        msg.edit_date = datetime.now(UTC)
        assert service.ingest(msg)
        assert db.get_item(item_id)["name"] == name
    # Even explicitly renaming to the original filename opts out of auto names.
    db.rename_item(item_id, "holiday.mp4")
    msg.message = "新的简介"
    msg.edit_date = datetime.now(UTC) + timedelta(seconds=1)
    assert not service.ingest(msg)
    assert db.get_item(item_id)["name"] == "holiday.mp4"
    db.close()


def test_schema6_caption_backfill_preserves_legacy_manual_names(workdir):
    path = workdir / "legacy-caption.sqlite3"
    db = Database(path)
    service = ChannelSyncService(db, ChannelStorage(), make_settings(workdir))
    msg = message(1, "holiday.mp4")
    service.ingest(msg)
    service.ingest(message(2, "2.mp4"))
    rows = db.conn.execute("SELECT message_id,item_id FROM channel_messages").fetchall()
    ids = {row["message_id"]: row["item_id"] for row in rows}
    db.rename_item(ids[2], "黑丝.mp4")
    db.conn.execute("ALTER TABLE channel_messages DROP COLUMN remote_name")
    db.conn.execute("DROP TABLE image_hosts")
    db.conn.execute("ALTER TABLE uploads DROP COLUMN image_host")
    db.conn.execute("PRAGMA user_version=6")
    db.conn.commit()
    db.close()
    db = Database(path)
    service = ChannelSyncService(db, ChannelStorage(), make_settings(workdir))
    msg.message = "频道视频简介"
    assert service.ingest(msg)
    assert not service.ingest(message(2, "2.mp4", caption="另一个简介"))
    assert db.get_item(ids[1])["name"] == "频道视频简介.mp4"
    assert db.get_item(ids[2])["name"] == "黑丝.mp4"
    db.close()


def test_legacy_manual_name_matching_new_caption_is_not_adopted(workdir):
    db = Database(workdir / "manual-caption.sqlite3")
    service = ChannelSyncService(db, ChannelStorage(), make_settings(workdir))
    msg = message(1, "holiday.mp4", caption="手动标题")
    service.ingest(msg)
    item_id = db.list_items(None)[0]["id"]
    db.rename_item(item_id, "手动标题.mp4")
    # This schema6 filename could never have used the old numeric-name rule.
    db.conn.execute("UPDATE channel_messages SET remote_name=NULL")
    db.conn.commit()
    assert not service.ingest(msg)
    msg.message = "修改后的简介"
    msg.edit_date = datetime.now(UTC)
    assert not service.ingest(msg)
    assert db.get_item(item_id)["name"] == "手动标题.mp4"
    db.close()


def test_thumbnail_requires_auth_and_does_not_download_video(authenticated_client):
    client, app, storage = authenticated_client
    app.state.db.import_channel_file(str(storage.chat), 1, describe_message(message(1)))
    item_id = app.state.db.conn.execute("SELECT id FROM items").fetchone()[0]
    calls = []

    async def thumbnail(mid):
        calls.append(mid)
        return b"\xff\xd8\xff\xd9"

    storage.thumbnail = thumbnail
    response = client.get(f"/api/files/{item_id}/thumbnail")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"].startswith("private")
    assert calls == [1]
    client.cookies.clear()
    assert client.get(f"/api/files/{item_id}/thumbnail").status_code == 401


def test_missing_telegram_cover_uses_bounded_server_sample(authenticated_client, monkeypatch):
    from app.api.routes import files
    from app.services.thumbnails import VIDEO_SAMPLE_BYTES

    client, app, storage = authenticated_client
    size = VIDEO_SAMPLE_BYTES * 2
    item = app.state.db.create_file(
        "sample.mp4",
        None,
        size,
        "video/mp4",
        None,
        str(storage.chat),
        [{"part_index": 0, "tg_message_id": 1, "size": size}],
    )

    async def missing_cover(_message_id):
        return None

    async def sample(_message_id, offset, length):
        assert offset == 0 and length == VIDEO_SAMPLE_BYTES
        yield bytes(length)

    async def render(data):
        assert len(data) == VIDEO_SAMPLE_BYTES
        return b"\xff\xd8\xff\xd9", "image/jpeg"

    storage.thumbnail = missing_cover
    storage.iter_download = sample
    monkeypatch.setattr(files, "video_thumbnail", render)
    result = client.get(f"/api/files/{item['id']}/thumbnail")
    assert result.status_code == 200
    assert result.content == b"\xff\xd8\xff\xd9"


def test_telegram_thumbnail_fetches_bytes_and_selects_static_size():
    from types import SimpleNamespace

    from telethon.tl.types import PhotoSize, PhotoStrippedSize

    from app.infrastructure.storage import TelegramStorage

    async def run():
        storage = TelegramStorage()
        thumb = PhotoSize(type="m", w=320, h=180, size=100)
        msg = SimpleNamespace(
            document=SimpleNamespace(thumbs=[PhotoStrippedSize("i", b"x"), thumb])
        )

        async def get_message(mid):
            return msg

        class Client:
            async def download_media(self, message, output, **kwargs):
                assert message is msg and output is bytes and kwargs["thumb"] is thumb
                return b"jpeg"

        storage._get_message = get_message
        storage.require_client = lambda: Client()
        assert await storage.thumbnail(1) == b"jpeg"
        msg.document.thumbs = []
        assert await storage.thumbnail(1) is None

    asyncio.run(run())
