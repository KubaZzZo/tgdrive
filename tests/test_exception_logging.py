from __future__ import annotations

import asyncio
import io
import logging
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest
from conftest import make_settings
from fastapi import HTTPException

from app.infrastructure import storage as storage_module
from app.services import accounts, channel_sync, uploads


@pytest.fixture()
def capture_logger(monkeypatch, caplog):
    def capture(module):
        logger = logging.Logger(module.logger.name, level=logging.WARNING)
        logger.addHandler(caplog.handler)
        monkeypatch.setattr(module, "logger", logger)
        return caplog.records

    return capture


@pytest.mark.parametrize("full", [True, False])
def test_sync_loop_logs_failure_and_preserves_retry_mode(workdir, capture_logger, full):
    records = capture_logger(channel_sync)
    service = channel_sync.ChannelSyncService(
        SimpleNamespace(), SimpleNamespace(chat=-100123), make_settings(workdir)
    )
    service._full = full
    service._wake = SimpleNamespace(wait=AsyncMock(return_value=True), clear=Mock())
    failure = ConnectionError("scan failed")
    service.scan = AsyncMock(side_effect=[failure, asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service._loop())
    assert service.scan.await_args_list == [call(full), call(full)]
    assert len(records) == 1
    assert "will retry chat_id=-100123" in records[0].getMessage()
    assert records[0].exc_info[1] is failure
    assert records[0].exc_info[2] is not None


def make_manager(workdir):
    control = SimpleNamespace(path="control", telegram_profiles=Mock(return_value=[{}]))
    storage = SimpleNamespace(chat=-100123, status=lambda: {"connected": True})
    runtime = SimpleNamespace(db=control, storage=storage, cleanup=SimpleNamespace(start=Mock()))
    return accounts.AccountManager(SimpleNamespace(), make_settings(workdir), control, runtime)


def test_account_identity_failure_logs_and_keeps_startup_available(workdir, capture_logger):
    records = capture_logger(accounts)
    manager = make_manager(workdir)
    manager.current.storage.me = AsyncMock(side_effect=ConnectionError("identity failed"))
    asyncio.run(manager.start())
    assert manager.identity is None
    assert len(records) == 1
    assert "identity at startup" in records[0].getMessage()
    assert records[0].exc_info[0] is ConnectionError


def test_account_startup_cancellation_is_not_swallowed_or_warned(workdir, capture_logger):
    records = capture_logger(accounts)
    manager = make_manager(workdir)
    manager.current.storage.me = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(manager.start())
    assert not records


@pytest.mark.parametrize("operation", ["authorization", "revocation"])
def test_unused_session_failure_logs_and_still_disconnects_and_cleans_files(
    workdir, capture_logger, operation
):
    records = capture_logger(accounts)
    manager = make_manager(workdir)
    client = SimpleNamespace(
        is_user_authorized=AsyncMock(return_value=True),
        log_out=AsyncMock(),
        disconnect=AsyncMock(),
    )
    method = client.is_user_authorized if operation == "authorization" else client.log_out
    failure = ConnectionError("session cleanup failed")
    method.side_effect = failure
    manager.pending = {"id": "a" * 32, "client": client, "account": None}
    directory = manager.settings.data_dir / "accounts"
    directory.mkdir(parents=True)
    paths = [
        directory / ("a" * 32 + suffix)
        for suffix in (".session", ".session-journal", ".session-wal", ".session-shm")
    ]
    for path in paths:
        path.touch()
    asyncio.run(manager._cancel())
    client.disconnect.assert_awaited_once()
    assert manager.pending is None
    assert not any(path.exists() for path in paths)
    assert len(records) == 1
    assert "unused Telegram session" in records[0].getMessage()
    assert records[0].exc_info[1] is failure


@pytest.mark.parametrize("operation", ["list", "activate"])
def test_channel_rpc_errors_log_and_keep_http_error_contract(
    workdir, capture_logger, monkeypatch, operation
):
    records = capture_logger(accounts)
    manager = make_manager(workdir)
    failure = ConnectionError("channel RPC failed")

    async def dialogs(**_kwargs):
        raise failure
        yield  # pragma: no cover - preserve the async-iterator interface

    manager.pending = {
        "id": "a" * 32,
        "state": "ready",
        "client": SimpleNamespace(iter_dialogs=dialogs),
    }
    monkeypatch.setattr(manager, "_entity", AsyncMock(side_effect=failure))
    operation = (
        manager.channels("a" * 32)
        if operation == "list"
        else manager.activate_login("a" * 32, "-100123")
    )
    with pytest.raises(HTTPException) as error:
        asyncio.run(operation)
    assert error.value.status_code == 502
    assert len(records) == 1
    assert records[0].exc_info[1] is failure


@pytest.mark.parametrize("operation", ["write", "hash"])
@pytest.mark.parametrize("failed", [True, False])
def test_canceled_upload_logs_worker_failure_once_and_preserves_cancellation(
    workdir, capture_logger, monkeypatch, operation, failed
):
    records = capture_logger(uploads)

    async def run():
        loop = asyncio.get_running_loop()
        entered, release = asyncio.Event(), Event()

        def background(*_args):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(5):
                raise TimeoutError("test did not release worker")
            if failed:
                raise OSError("background worker failed")
            return "checksum"

        if operation == "write":
            space = uploads.UploadSpace()
            monkeypatch.setattr(space, "_write", background)
            task = asyncio.create_task(space.write(workdir, io.BytesIO(), b"data"))
        else:
            monkeypatch.setattr(uploads, "file_sha256", background)
            task = asyncio.create_task(uploads.upload_sha256(workdir / "upload.part"))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    if failed:
        assert len(records) == 1
        assert "while canceling upload" in records[0].getMessage()
        assert records[0].exc_info[0] is OSError
        assert records[0].exc_info[2] is not None
    else:
        assert not records


def test_failed_storage_disconnect_logs_without_masking_auth_error(
    workdir, capture_logger, monkeypatch
):
    records = capture_logger(storage_module)
    client = SimpleNamespace(
        connect=AsyncMock(),
        get_me=AsyncMock(return_value=None),
        disconnect=AsyncMock(side_effect=ConnectionError("disconnect failed")),
    )
    monkeypatch.setattr(storage_module, "TelegramClient", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(storage_module, "get_proxy", lambda: None)
    storage = storage_module.TelegramStorage(make_settings(workdir))
    asyncio.run(storage.start())
    assert storage.auth_required
    assert storage.client is None
    assert len(records) == 1
    assert "disconnect failed Telegram client" in records[0].getMessage()
    assert records[0].exc_info[0] is ConnectionError
