from __future__ import annotations

import asyncio
import json
from contextlib import aclosing
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

from conftest import FakeTelegramStorage, make_settings
from fastapi.testclient import TestClient
from telethon.tl.types import Channel, ChatAdminRights, ChatPhotoEmpty

from app.main import create_app
from app.services.progress_events import ProgressSignal, progress_messages

PASSWORD = "correct-horse-battery-staple"


def add_file(db, storage, name, message_id, parent_id=None):
    data = b"test-file"
    storage.files[message_id] = data
    return db.create_file(
        name,
        parent_id,
        len(data),
        "application/octet-stream",
        None,
        str(storage.chat),
        [{"part_index": 0, "tg_message_id": message_id, "size": len(data)}],
    )


def test_channel_restore_previews_then_merges_without_overwriting(authenticated_client, workdir):
    client, app, storage = authenticated_client
    source_storage = FakeTelegramStorage()
    source = create_app(make_settings(workdir / "archive"), storage=source_storage)
    folder = source.state.db.create_folder("Projects", None)
    fresh = add_file(source.state.db, source_storage, "new.bin", 200, folder["id"])
    add_file(source.state.db, source_storage, "collision.bin", 201)
    trashed = add_file(source.state.db, source_storage, "trashed.bin", 202)
    source.state.db.trash_tree(trashed["id"])
    source.state.db.create_share(fresh["id"], "source-share", None)
    with TestClient(source) as exporter:
        assert exporter.post("/api/login", json={"password": PASSWORD}).is_success
        backup = exporter.get("/api/clients/index-backup").content

    occupied = add_file(app.state.db, storage, "collision.bin", 300)
    foreign_storage = FakeTelegramStorage()
    foreign_storage.chat = -1000000000242
    add_file(app.state.db, foreign_storage, "foreign-workspace.bin", 200)
    prefix = "/api/clients/index-restore"

    def preview(query=""):
        return client.post(prefix + "/preview" + query, content=backup)

    assert client.post(prefix + "/confirm", content=backup).status_code == 400
    result = preview()
    assert result.status_code == 200, result.text
    plan = result.json()
    assert plan["channel_id"] == str(storage.chat)
    assert plan["added"]["items"] == 2
    assert plan["added"]["file_parts"] == 1
    assert plan["conflict_count"] == 1
    assert plan["skipped_trash"] == 1 and plan["skipped_links"] == 1
    assert app.state.db.get_item(fresh["id"]) is None

    temporary = app.state.db.create_folder("Projects", None)
    headers = {"X-Restore-Preview": plan["preview_id"]}
    assert client.post(prefix + "/confirm", headers=headers, content=backup).status_code == 409
    assert app.state.db.get_item(fresh["id"]) is None
    app.state.db.trash_tree(temporary["id"])
    result = client.post(prefix + "/confirm", headers=headers, content=backup)
    assert result.status_code == 200, result.text
    assert app.state.db.get_item(fresh["id"])["parent_id"] == folder["id"]
    assert app.state.db.get_item(occupied["id"])["name"] == "collision.bin"
    assert app.state.db.get_item(trashed["id"], include_trash=True) is None
    assert app.state.db.get_share("source-share") is None
    assert client.post(prefix + "/confirm", headers=headers, content=backup).status_code == 409
    with_links = preview("?include_links=true").json()
    assert with_links["added"]["shares"] == 1
    assert client.post(
        prefix + "/confirm?include_links=true",
        content=backup,
        headers={"X-Restore-Preview": with_links["preview_id"]},
    ).is_success
    assert app.state.db.get_share("source-share") is not None
    storage.chat = -1000000000500
    assert preview().status_code == 422
    assert client.post(prefix + "/preview", content=b"bad ZIP").status_code == 422


def test_telegram_diagnostics_probes_permissions_and_separate_channel(authenticated_client):
    client, app, storage = authenticated_client
    manager = app.state.accounts
    runtime = manager.current
    test_chat = -1000000000500
    manager.settings = replace(manager.settings, tg_diagnostic_chat_id=test_chat)
    runtime.sync.last_error = "示例同步错误"
    calls = []

    async def get_me():
        return SimpleNamespace(id=10)

    async def get_entity(chat):
        assert chat == storage.chat
        return Channel(
            id=1234567890,
            title="Storage",
            photo=ChatPhotoEmpty(),
            date=datetime.now(UTC),
            broadcast=True,
            admin_rights=ChatAdminRights(post_messages=True, delete_messages=False),
        )

    async def bot_call(method, **options):
        calls.append((method, options))
        return {
            "getMe": {"id": 20},
            "getChatMember": {"status": "administrator", "can_post_messages": True},
            "getChat": {"id": test_chat, "type": "channel"},
            "sendMessage": {"message_id": 8},
        }[method]

    storage.client = SimpleNamespace(get_me=get_me, get_entity=get_entity)
    runtime.sync.bot = SimpleNamespace(call=bot_call)
    endpoint = "/api/telegram/diagnostics"
    result = client.get(endpoint).json()
    assert result["session"]["can_post"] is True
    assert result["session"]["can_delete"] is False
    assert result["bot"]["can_post"] is True
    assert result["sync"]["error"] == "示例同步错误"
    assert result["test_channel_id"] == str(test_chat)
    assert [call[0] for call in calls] == ["getMe", "getChatMember"]
    assert (
        client.post(endpoint + "/test-message", json={"chat_id": str(test_chat - 1)}).status_code
        == 409
    )
    assert [call[0] for call in calls] == ["getMe", "getChatMember"]
    assert client.post(endpoint + "/test-message", json={"chat_id": str(test_chat)}).is_success
    assert calls[-1][0] == "sendMessage" and calls[-1][1]["chat_id"] == test_chat
    manager.settings = replace(manager.settings, tg_diagnostic_chat_id=storage.chat)
    before = len(calls)
    assert (
        client.post(endpoint + "/test-message", json={"chat_id": str(storage.chat)}).status_code
        == 409
    )
    assert len(calls) == before


def test_terminal_task_streams_check_workspace_and_hide_private_fields(authenticated_client):
    client, app, storage = authenticated_client
    db = app.state.db
    upload = db.create_upload(None, "test.bin", 8, None, "private.part", 8)
    db.update_upload(upload["id"], state="done")
    url = f"/api/uploads/{upload['id']}/events"
    upload_events = client.get(url, params={"workspace_id": storage.chat})
    assert upload_events.status_code == 200
    assert upload_events.headers["content-type"].startswith("text/event-stream")
    assert "no-transform" in upload_events.headers["Cache-Control"]
    assert json.loads(upload_events.text.removeprefix("data: "))["state"] == "done"
    assert "private.part" not in upload_events.text
    assert client.get(url, params={"workspace_id": "-1000000000977"}).status_code == 409

    app.state.video_import.batch = {
        "directory": "imports/example",
        "workspace": db.path,
        "cancel": False,
        "jobs": [{"id": "job", "state": "done", "url": "https://private.example"}],
    }
    batch_events = client.get("/api/video-import/example/events")
    assert batch_events.status_code == 200
    assert "private.example" not in batch_events.text
    assert json.loads(batch_events.text.removeprefix("data: "))["jobs"][0]["state"] == "done"
    app.state.video_import.batch["workspace"] = "elsewhere"
    assert client.get("/api/video-import/example/events").status_code == 404
    assert client.get("/api/video-import").json()["jobs"] == []


def test_progress_signal_yields_only_changed_snapshots_and_releases_subscription():
    async def run():
        signal = ProgressSignal()
        amount = 0

        async with aclosing(
            progress_messages(
                lambda: {"amount": amount}, lambda state: state["amount"] == 8, signal
            )
        ) as stream:
            assert b'{"amount":0}' in await anext(stream)
            pending = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            amount = 8
            signal.notify()
            assert b'{"amount":8}' in await asyncio.wait_for(pending, timeout=1)
        assert not signal.listeners

    asyncio.run(run())


def test_share_page_uses_an_allowlisted_theme_cookie(authenticated_client):
    client, app, storage = authenticated_client
    file = add_file(app.state.db, storage, "share.bin", 500)
    share = client.post(f"/api/items/{file['id']}/share", json={}).json()["token"]
    assert 'data-theme="system"' in client.get(f"/s/{share}").text
    client.cookies.set("tgdrive_theme", "dark")
    assert 'data-theme="dark"' in client.get(f"/s/{share}").text
    client.cookies.set("tgdrive_theme", "invalid")
    assert 'data-theme="system"' in client.get(f"/s/{share}").text
