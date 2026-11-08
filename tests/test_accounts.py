from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from conftest import FakeTelegramStorage, make_settings
from fastapi import HTTPException
from fastapi.testclient import TestClient
from telethon import functions, types, utils
from telethon.errors import (
    AuthKeyUnregisteredError,
    PasswordHashInvalidError,
    SessionPasswordNeededError,
)
from telethon.tl.functions.messages import GetHistoryRequest
from telethon.tl.types import Channel, ChatPhotoEmpty, InputPeerSelf

import app.services.accounts as module
from app.main import create_app
from app.services.accounts import WorkspaceMiddleware, account_settings


def channel(identifier=222, creator=True):
    return Channel(
        id=identifier,
        title=f"Channel {identifier}",
        photo=ChatPhotoEmpty(),
        date=datetime.now(UTC),
        broadcast=True,
        creator=creator,
    )


class Client:
    instances = []
    two_factor = False
    fail_connect = False
    lookup_miss = True
    migrate = False
    fast_first_expiry = False

    def __init__(self, *args, **kwargs):
        self.instances.append(self)
        self.scanned = False
        self.scan_handler = None
        self.migrated = False
        self.disconnected = False
        self.revoked = False
        self.exports = 0
        self.identity_reads = 0
        self.user = SimpleNamespace(id=42, username="second", first_name="Second", bot=False)

    async def connect(self):
        if self.fail_connect:
            raise OSError("network")

    async def disconnect(self):
        self.disconnected = True

    async def log_out(self):
        self.revoked = True

    async def is_user_authorized(self):
        return True

    async def get_me(self):
        self.identity_reads += 1
        return self.user

    def add_event_handler(self, handler, _event):
        self.scan_handler = handler

    def remove_event_handler(self, _handler):
        self.scan_handler = None

    async def scan(self, notify=True):
        self.scanned = True
        if notify:
            await self.scan_handler(None)

    async def __call__(self, request):
        if isinstance(request, functions.auth.ExportLoginTokenRequest):
            self.exports += 1
            if not self.scanned:
                ttl = 0 if self.fast_first_expiry and self.exports == 1 else 45
                expires = datetime.now(UTC) + timedelta(seconds=ttl)
                return types.auth.LoginToken(expires, b"test-token")
            if self.migrate and not self.migrated:
                return types.auth.LoginTokenMigrateTo(2, b"migration-token")
        else:
            assert isinstance(request, functions.auth.ImportLoginTokenRequest)
            assert self.migrated and request.token == b"migration-token"
        if self.two_factor:
            raise SessionPasswordNeededError(None)
        return types.auth.LoginTokenSuccess(SimpleNamespace(user=self.user))

    async def _switch_dc(self, dc):
        assert dc == 2
        self.migrated = True

    async def _on_login(self, user):
        assert user.id == 42

    async def sign_in(self, password):
        if password != " correct password ":
            raise PasswordHashInvalidError(None)
        return self.user

    async def iter_dialogs(self, **kwargs):
        yield SimpleNamespace(entity=channel(), name="Channel 222")
        yield SimpleNamespace(entity=channel(333, False), name="Read only")

    async def get_entity(self, target):
        if target == "@denied":
            return channel(333, False)
        if target == -1000000000222 and self.lookup_miss:
            raise ValueError("Not in session entity cache")
        if isinstance(target, int):
            return channel(utils.resolve_id(target)[0])
        return channel()


class Sync:
    fail_start = False

    def __init__(self, *args):
        self.state = "idle"
        self._full = True

    async def start(self):
        if self.fail_start:
            raise OSError("subscribe failed")
        self.state = "idle"

    async def shutdown(self):
        self.state = "stopped"


class Storage(FakeTelegramStorage):
    mode = "user"

    def __init__(self, chat):
        super().__init__()
        self.chat, self.started = chat, True


def profile(chat, identifier="a" * 32):
    return {
        "id": identifier,
        "session_key": identifier,
        "chat_id": str(chat),
        "user_id": 42,
        "username": "second",
        "first_name": "Second",
        "title": "Other",
    }


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.fixture()
def fake_telegram(monkeypatch):
    Client.instances = []
    Client.two_factor = False
    Client.fail_connect = False
    Client.lookup_miss = True
    Client.migrate = False
    Client.fast_first_expiry = False
    monkeypatch.setattr(module, "TelegramClient", Client)
    monkeypatch.setattr(module, "ChannelSyncService", Sync)


def test_accounts_require_auth_and_password_validation_does_not_echo(app_client):
    client, _, _ = app_client
    assert client.get("/api/telegram").status_code == 401
    assert client.post("/api/telegram/login").status_code == 401
    assert client.post("/api/telegram/profiles/default/activate").status_code == 401
    assert client.get("/api/items", headers={"X-Workspace-ID": "old"}).status_code == 401
    client.post("/api/login", json={"password": "correct-horse-battery-staple"})
    assert client.get("/api/telegram").json()["active_id"] == "default"
    secret = "never-echo-this" * 50
    response = client.post("/api/telegram/login/missing/password", json={"password": secret})
    assert response.status_code == 422 and secret not in response.text
    assert "input" not in response.json()["errors"][0]


def test_qr_refresh_2fa_channel_permissions_and_activation(workdir, fake_telegram):
    async def run():
        app = create_app(make_settings(workdir), storage=FakeTelegramStorage())
        manager = app.state.accounts
        app.state.storage.started = True
        await manager.start()
        Client.two_factor = True
        Client.migrate = True
        Client.fast_first_expiry = True
        login = await manager.begin()
        await until(lambda: manager.pending["state"] == "waiting")
        client = Client.instances[-1]
        await until(lambda: client.exports == 2 and manager.pending["state"] == "waiting")
        assert manager.login_status(login["id"])["qr_image"].startswith("data:image/png;base64,")
        assert "client" not in manager.login_status(login["id"])
        await client.scan()
        await until(lambda: manager.pending["state"] == "password")
        assert client.migrated
        with pytest.raises(HTTPException, match="400"):
            await manager.password(login["id"], "wrong")
        result = await manager.password(login["id"], " correct password ")
        assert result["state"] == "ready"
        assert client.identity_reads == 0
        choices = await manager.channels(login["id"])
        assert [c["writable"] for c in choices] == [True, False]
        with pytest.raises(HTTPException, match="400"):
            await manager.activate_login(login["id"], "@denied")
        assert manager.status()["active_id"] == "default"
        result = await manager.activate_login(login["id"], choices[0]["id"])
        assert result["active_id"] == login["id"]
        assert result["chat_id"] == str(utils.get_peer_id(channel()))
        assert manager.pending is None and not client.revoked
        assert all("session_key" not in p for p in result["profiles"])
        await manager.shutdown()

    asyncio.run(run())


def test_channel_workspaces_restore_original_files_and_global_traffic(workdir, fake_telegram):
    async def run():
        app = create_app(make_settings(workdir), storage=Storage(-100111))
        manager = app.state.accounts
        await manager.start()
        root = app.state.db
        original = root.create_folder("Original folder", None)
        shared = root.create_file("Original.txt", None, 3, "text/plain", None, "-100111", [])
        root.create_share(shared["id"], "original-share-token", None)
        root.update_traffic_config(90_000_000_000)
        root.record_traffic([("2026-11-04", "web", 100, 200)])
        target = profile(-100222)
        await manager._activate(Storage(-100222), target)
        other = app.state.db
        assert other is not root and other.list_items(None) == []
        assert other.get_share("original-share-token") is None
        second = other.create_folder("Second folder", None)
        assert app.state.traffic.db is root
        assert app.state.traffic.db.traffic_config()["quota_bytes"] == 90_000_000_000
        original_profile = next(p for p in root.telegram_profiles() if p["id"] == "default")
        await manager._activate(Storage(-100111), original_profile)
        assert app.state.db is root and root.get_item(original["id"])
        assert root.get_share("original-share-token")["item_id"] == shared["id"]
        assert root.get_item(second["id"]) is None
        await manager._activate(Storage(-100222), target)
        assert app.state.db is other and other.get_item(second["id"])
        assert other.get_item(original["id"]) is None
        await manager.shutdown()

    asyncio.run(run())


def test_activation_refuses_processing_upload_and_rolls_back_on_failure(
    workdir, fake_telegram, monkeypatch
):
    async def run():
        app = create_app(make_settings(workdir), storage=Storage(-100111))
        manager = app.state.accounts
        await manager.start()
        old = manager.current
        old.uploads._tasks["busy"] = object()
        with pytest.raises(HTTPException, match="409"):
            await manager._activate(Storage(-100222), profile(-100222))
        old.uploads._tasks.clear()
        with monkeypatch.context() as patch:
            patch.setattr(Sync, "fail_start", True)
            with pytest.raises(OSError):
                await manager._activate(Storage(-100222), profile(-100222))
        assert manager.current is old
        assert not old.uploads._closed and not manager.switching
        assert not old.storage.stopped
        assert manager.control.active_telegram_profile()["id"] == "default"
        await manager.shutdown()

    asyncio.run(run())


def test_existing_request_keeps_original_storage_until_response_finishes(workdir, fake_telegram):
    async def run():
        app = create_app(make_settings(workdir), storage=Storage(-100111))
        manager = app.state.accounts
        await manager.start()
        old = manager.current
        seen = []

        async def request(scope, receive, send):
            await manager._activate(Storage(-100222), profile(-100222))
            assert scope["state"]["telegram_runtime"] is old
            assert not old.storage.stopped
            seen.append(True)

        await WorkspaceMiddleware(request, manager)(
            {"type": "http", "path": "/", "headers": []}, None, None
        )
        await until(lambda: old.storage.stopped)
        assert seen and old.retired
        await manager.shutdown()

    asyncio.run(run())


def test_stale_browser_workspace_and_native_download_are_rejected(authenticated_client):
    client, _, _ = authenticated_client
    response = client.post(
        "/api/folders", headers={"X-Workspace-ID": "old-channel"}, json={"name": "Wrong"}
    )
    assert response.status_code == 409 and response.json()["workspace_changed"]
    assert client.get("/api/files/missing/download?workspace_id=old-channel").status_code == 409
    assert client.get("/api/items").json()["items"] == []


def test_cancelled_login_disconnects_and_removes_only_pending_session(workdir, fake_telegram):
    async def run():
        app = create_app(make_settings(workdir), storage=Storage(-100111))
        manager = app.state.accounts
        await manager.start()
        login = await manager.begin()
        await until(lambda: manager.pending["state"] == "waiting")
        client = Client.instances[-1]
        await client.scan()
        await until(lambda: manager.pending["state"] == "ready")
        path = manager.settings.data_dir / "accounts" / (login["id"] + ".session")
        path.write_text("fake-pending-session")
        await manager.cancel(login["id"])
        assert client.revoked and client.disconnected and not path.exists()
        assert manager.current.storage.chat == -100111
        assert manager.control.active_telegram_profile()["id"] == "default"
        await manager.shutdown()

    asyncio.run(run())


def test_saved_connection_and_workspace_survive_application_restart(
    workdir, fake_telegram, monkeypatch
):
    async def setup():
        settings = make_settings(workdir)
        app = create_app(settings, storage=Storage(-100111))
        manager = app.state.accounts
        await manager.start()
        target = profile(-100222)
        await manager._activate(Storage(-100222), target)
        item = app.state.db.create_folder("Persists", None)
        await manager.shutdown()
        return settings, target, item

    settings, target, item = asyncio.run(setup())
    monkeypatch.setattr("app.storage.TelegramClient", Client)
    # Startup restoration must select the workspace before serving any requests.
    app = create_app(settings)
    assert app.state.db.get_item(item["id"])["name"] == "Persists"
    assert app.state.storage.settings.tg_session.name == target["session_key"]
    assert app.state.storage.settings.tg_chat_id == target["chat_id"]
    asyncio.run(app.state.accounts.shutdown())


def test_session_settings_reject_untrusted_paths(workdir):
    settings = make_settings(workdir)
    with pytest.raises(RuntimeError):
        account_settings(settings, {"session_key": "../../etc/passwd", "chat_id": "-100123"})
    assert (
        account_settings(settings, {"session_key": "", "chat_id": "-100999"}).tg_chat_id
        == "-100999"
    )


def test_saved_connection_switch_reconnects_and_keeps_workspace(
    workdir, fake_telegram, monkeypatch
):
    monkeypatch.setattr("app.storage.TelegramClient", Client)
    Client.lookup_miss = False

    async def run():
        first_id = utils.get_peer_id(channel(111))
        second_id = utils.get_peer_id(channel(222))
        app = create_app(make_settings(workdir), storage=Storage(first_id))
        manager = app.state.accounts
        await manager.start()
        folder = app.state.db.create_folder("Kept", None)
        target = profile(second_id)
        await manager._activate(Storage(second_id), target)
        await asyncio.sleep(0)
        result = await manager.activate_saved("default")
        assert result["active_id"] == "default" and result["connected"]
        assert app.state.db.get_item(folder["id"])
        await manager.activate_saved(target["id"])
        assert app.state.db.get_item(folder["id"]) is None
        assert (await manager.activate_saved(target["id"]))["active_id"] == target["id"]
        with pytest.raises(HTTPException, match="404"):
            await manager.activate_saved("missing")
        await manager.shutdown()

    asyncio.run(run())


def test_revoked_history_session_requires_qr_login_and_recovers_same_workspace(
    workdir, monkeypatch
):
    class HistoryClient(Client):
        lookup_miss = False
        reject_history = False
        latest_id = 1

        def add_event_handler(self, callback, builder):
            self.scan_handler = callback

        def remove_event_handler(self, callback, builder=None):
            self.scan_handler = None

        async def get_messages(self, chat, *, limit):
            if self.reject_history:
                request = GetHistoryRequest(InputPeerSelf(), 0, None, 0, limit, 0, 0, 0)
                raise AuthKeyUnregisteredError(request)
            return [SimpleNamespace(id=self.latest_id)]

        async def iter_messages(self, chat, **kwargs):
            for mid in range(kwargs["min_id"] + 1, kwargs["max_id"]):
                yield SimpleNamespace(id=mid)

    async def run():
        chat_id = str(utils.get_peer_id(channel()))
        settings = make_settings(workdir, tg_chat_id=chat_id)
        app = create_app(settings)
        manager = app.state.accounts
        await manager.current.storage.start()
        await manager.start()
        await manager.current.sync.start()
        try:
            workspace = manager.current.db
            folder = workspace.create_folder("Existing folder", None)
            active_id = manager.status()["active_id"]
            for index in range(9):
                manager.control.save_telegram_profile(
                    profile(-100333, f"{index:032x}"), "unused", activate=False
                )
            await until(lambda: manager.current.sync.last_success is not None)
            assert manager.status()["connected"]
            manager.current.storage.client.reject_history = True
            assert manager.current.sync.request_scan(full=True)
            await until(lambda: manager.current.sync.last_error is not None)
            assert not manager.status()["connected"]
            assert manager.current.sync.state == "auth_required"
            assert "重新扫码" in manager.current.sync.last_error
            assert not manager.current.sync.request_scan(full=True)
            assert workspace.channel_cursor(chat_id) == 1
            with pytest.raises(HTTPException, match="重新扫码"):
                await manager.activate_saved(active_id)

            login = await manager.begin()
            await until(lambda: manager.pending["state"] == "waiting")
            await manager.pending["client"].scan()
            manager.pending["client"].latest_id = 2
            await until(lambda: manager.pending["state"] == "ready")
            result = await manager.activate_login(login["id"], chat_id)
            assert result["connected"] and result["active_id"] == active_id
            assert len(result["profiles"]) == 10
            await until(lambda: manager.current.sync.last_success is not None)
            assert manager.current.db is workspace and workspace.get_item(folder["id"])
            assert workspace.channel_cursor(chat_id) == 2
        finally:
            await manager.shutdown()

    monkeypatch.setattr("app.storage.TelegramClient", HistoryClient)
    monkeypatch.setattr(module, "TelegramClient", HistoryClient)
    asyncio.run(run())


def test_authenticated_api_flow_keeps_traffic_settings_global(workdir, fake_telegram):
    import time

    app = create_app(make_settings(workdir), storage=Storage(-100111))
    with TestClient(app) as client:
        client.post("/api/login", json={"password": "correct-horse-battery-staple"})
        login = client.post("/api/telegram/login").json()
        job_id = login["id"]
        deadline = time.monotonic() + 2
        while client.get(f"/api/telegram/login/{job_id}").json()["state"] != "waiting":
            assert time.monotonic() < deadline
        client.portal.call(Client.instances[-1].scan)
        while client.get(f"/api/telegram/login/{job_id}").json()["state"] != "ready":
            assert time.monotonic() < deadline
        channels = client.get(f"/api/telegram/login/{job_id}/channels").json()["channels"]
        result = client.post(
            f"/api/telegram/login/{job_id}/activate", json={"chat_id": channels[0]["id"]}
        )
        assert result.status_code == 200
        assert client.get("/api/meta").json()["workspace_is_default"] is False
        response = client.put("/api/traffic/settings", json={"quota_bytes": 80_000_000_000})
        assert response.status_code == 200
        assert app.state.accounts.control.traffic_config()["quota_bytes"] == 80_000_000_000
        assert app.state.db.traffic_config()["quota_bytes"] == 100_000_000_000
        assert client.get(f"/api/telegram/login/{job_id}").status_code == 404


def test_manual_scan_check_recovers_two_factor_without_update(workdir, fake_telegram):
    import time

    Client.two_factor = True
    app = create_app(make_settings(workdir), storage=Storage(-100111))
    with TestClient(app) as client:
        client.post("/api/login", json={"password": "correct-horse-battery-staple"})
        job_id = client.post("/api/telegram/login").json()["id"]
        path = f"/api/telegram/login/{job_id}"

        def wait_for(state):
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                job = client.get(path).json()
                if job["state"] == state:
                    return job
                time.sleep(0.01)
            pytest.fail(f"Login did not reach {state}")

        wait_for("waiting")
        assert client.post(f"{path}/password", json={"password": "not-yet"}).status_code == 409
        client.portal.call(Client.instances[-1].scan, False)
        assert client.post(f"{path}/check").status_code == 202
        assert wait_for("password")["qr_image"] is None
        assert client.post(f"{path}/password", json={"password": "wrong"}).status_code == 400
        assert client.get(path).json()["state"] == "password"
        verified = client.post(f"{path}/password", json={"password": " correct password "})
        assert verified.status_code == 200 and verified.json()["state"] == "ready"
        assert client.get(f"{path}/channels").json()["channels"]


def test_expired_login_and_network_failure_leave_current_connection(workdir, fake_telegram):
    async def run():
        app = create_app(make_settings(workdir), storage=Storage(-100111))
        manager = app.state.accounts
        await manager.start()
        login = await manager.begin()
        await until(lambda: manager.pending["state"] == "waiting")
        async with manager.lock:
            await manager._cancel(expired=True)
        assert manager.login_status(login["id"])["state"] == "expired"
        Client.fail_connect = True
        second = await manager.begin()
        await until(lambda: manager.pending["state"] == "error")
        assert manager.login_status(second["id"])["error"]
        assert manager.current.storage.chat == -100111 and manager.current.storage.started
        await manager.shutdown()

    asyncio.run(run())
