from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from conftest import FakeTelegramStorage, make_settings
from fastapi.testclient import TestClient
from telethon.errors import AuthKeyUnregisteredError

from app.db import Database
from app.main import create_app
from app.services import telegram_bot
from app.services.channel_sync import describe_bot_post


@pytest.mark.parametrize("session_starts_connected", [False, True])
@pytest.mark.parametrize("size", [5, telegram_bot.MAX_BOT_DOWNLOAD + 1])
def test_bot_fallback_imports_once_and_serves_byte_ranges(
    workdir, monkeypatch, session_starts_connected, size
):
    class Storage(FakeTelegramStorage):
        async def start(self):
            self.started = session_starts_connected

        async def subscribe_channel(self, *_callbacks):
            return lambda: None

        async def latest_channel_message_id(self):
            raise AuthKeyUnregisteredError(None)

        def invalidate_session(self):
            self.started = False
            self.error = "Session expired"

    storage = Storage()
    post = {
        "chat": {"id": storage.chat},
        "message_id": 5,
        "date": 1_780_000_000,
        "video": {
            "file_id": "bot-file",
            "file_size": size,
            "file_name": "example.mp4",
            "cover": [{"file_id": "cover-file", "width": 320, "height": 180}],
        },
    }
    original = {
        **post,
        "caption": "人物：张三",
        "video": {k: v for k, v in post["video"].items() if k != "cover"},
    }
    offsets = []

    async def telegram(request):
        method = request.url.path.rsplit("/", 1)[-1]
        params = json.loads(request.content) if request.content else {}
        if method == "getChatMember":
            assert params == {"chat_id": storage.chat, "user_id": 123}
            return httpx.Response(200, json={"ok": True, "result": {"status": "administrator"}})
        if method == "getUpdates":
            offsets.append(params.get("offset"))
            if len(offsets) > 1:
                await asyncio.Event().wait()
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        {"update_id": 1, "channel_post": original},
                        {"update_id": 2, "channel_post": original},
                        {"update_id": 3, "channel_post": {**post, "chat": {"id": -10099}}},
                        {
                            "update_id": 4,
                            "channel_post": {
                                **post,
                                "message_id": 6,
                                "caption": "tgdrive:v1:upload:0",
                            },
                        },
                        {
                            "update_id": 5,
                            "edited_channel_post": {
                                **post,
                                "caption": "人物：李四",
                                "edit_date": post["date"] + 1,
                            },
                        },
                    ],
                },
            )
        if method == "getFile":
            if params == {"file_id": "cover-file"}:
                return httpx.Response(
                    200, json={"ok": True, "result": {"file_path": "photos/cover"}}
                )
            assert params == {"file_id": "bot-file"}
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "documents/test"}})
        if method == "cover":
            return httpx.Response(200, content=b"\xff\xd8\xffcover")
        assert request.headers["range"] == "bytes=1-2"
        return httpx.Response(200, content=b"hello")

    client_factory = httpx.AsyncClient
    monkeypatch.setattr(
        telegram_bot.httpx,
        "AsyncClient",
        lambda **kwargs: client_factory(transport=httpx.MockTransport(telegram), **kwargs),
    )
    app = create_app(make_settings(workdir, tg_bot_fallback_token="123:test"), storage=storage)
    with TestClient(app) as client:
        client.post("/api/login", json={"password": "correct-horse-battery-staple"})
        deadline = time.monotonic() + 2
        while len(offsets) < 2:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        status = client.get("/api/sync").json()
        assert status["bot_state"] == "listening" and status["files"] == 1
        assert status["state"] == ("auth_required" if session_starts_connected else "unavailable")
        assert offsets == [None, 6]
        assert app.state.db.channel_cursor(str(storage.chat)) == 0
        ready = client.get("/api/health/ready")
        assert ready.status_code == 200 and ready.json()["checks"]["telegram"] == "bot_fallback"
        assert client.post("/api/sync", json={"full": True}).status_code == 409
        [item] = client.get("/api/items").json()["items"]
        assert client.get("/api/people").json() == {
            "people": [{"name": "李四", "display_name": "李四", "count": 1}],
            "total": 1,
        }
        thumbnail = client.get(f"/api/files/{item['id']}/thumbnail")
        assert thumbnail.status_code == 200 and thumbnail.content == b"\xff\xd8\xffcover"
        assert thumbnail.headers["content-type"] == "image/jpeg"
        url = f"/api/files/{item['id']}/download"
        if size > telegram_bot.MAX_BOT_DOWNLOAD:
            assert client.get(url, headers={"Range": "bytes=1-2"}).status_code == 503
            return
        assert client.head(url).headers["content-length"] == "5"
        response = client.get(url, headers={"Range": "bytes=1-2"})
        assert response.status_code == 206 and response.content == b"el"


def test_bot_file_is_reconciled_by_primary_without_losing_the_local_name(workdir):
    db = Database(workdir / "bot.sqlite3")
    try:
        descriptor, file_id, _cover_id = describe_bot_post(
            {
                "message_id": 5,
                "date": 1_780_000_000,
                "document": {"file_id": "bot-file", "file_size": 5, "file_name": "example.txt"},
            }
        )
        assert db.import_bot_file("-1001", 5, descriptor, "123", file_id)
        [item] = db.list_items(None)
        db.rename_item(item["id"], "custom.txt")
        db.import_channel_file("-1001", 5, {**descriptor, "media_key": "document:92"}, "scan")
        assert db.reconcile_channel("-1001", "scan", 5) == 0
        assert db.get_item(item["id"])["name"] == "custom.txt"
        assert db.bot_file_id(item["id"], "123") == file_id
        assert db.bot_file_id(item["id"], "456") is None
    finally:
        db.close()


def test_bot_video_replacement_updates_people_and_keeps_the_latest_file(workdir):
    db = Database(workdir / "bot.sqlite3")
    chat = "-1001"
    original = {
        "message_id": 5,
        "date": 1_780_000_000,
        "caption": "人物：张三",
        "video": {"file_id": "old-file", "file_size": 5, "file_name": "original.mp4"},
    }
    replacement = {
        **original,
        "edit_date": original["date"] + 1,
        "caption": "人物：李四",
        "video": {"file_id": "new-file", "file_size": 9, "file_name": "replacement.mp4"},
    }
    try:
        original_file, original_id, original_cover = describe_bot_post(original)
        replaced_file, replaced_id, replaced_cover = describe_bot_post(replacement)
        assert db.import_bot_file(chat, 5, original_file, "123", original_id, original_cover)
        [item] = db.list_items(None)
        assert db.import_bot_file(chat, 5, replaced_file, "123", replaced_id, replaced_cover)
        assert not db.import_bot_file(chat, 5, original_file, "123", original_id, original_cover)
        assert db.bot_file_id(item["id"], "123") == "new-file"
        assert db.get_parts(item["id"])[0]["size"] == 9
        assert db.list_video_people("", 10, 0) == (
            [{"name": "李四", "display_name": "李四", "count": 1}],
            1,
        )
    finally:
        db.close()
