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
def test_bot_fallback_imports_once_and_serves_byte_ranges(
    workdir, monkeypatch, session_starts_connected
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
        "document": {"file_id": "bot-file", "file_size": 5, "file_name": "example.txt"},
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
                        {"update_id": 1, "channel_post": post},
                        {"update_id": 2, "channel_post": post},
                        {"update_id": 3, "channel_post": {**post, "chat": {"id": -10099}}},
                        {
                            "update_id": 4,
                            "channel_post": {
                                **post,
                                "message_id": 6,
                                "caption": "tgdrive:v1:upload:0",
                            },
                        },
                    ],
                },
            )
        if method == "getFile":
            assert params == {"file_id": "bot-file"}
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "documents/test"}})
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
        assert offsets == [None, 5]
        assert app.state.db.channel_cursor(str(storage.chat)) == 0
        ready = client.get("/api/health/ready")
        assert ready.status_code == 200 and ready.json()["checks"]["telegram"] == "bot_fallback"
        assert client.post("/api/sync", json={"full": True}).status_code == 409
        [item] = client.get("/api/items").json()["items"]
        url = f"/api/files/{item['id']}/download"
        assert client.head(url).headers["content-length"] == "5"
        response = client.get(url, headers={"Range": "bytes=1-2"})
        assert response.status_code == 206 and response.content == b"el"


def test_bot_file_is_reconciled_by_primary_without_losing_the_local_name(workdir):
    db = Database(workdir / "bot.sqlite3")
    try:
        descriptor, file_id = describe_bot_post(
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
