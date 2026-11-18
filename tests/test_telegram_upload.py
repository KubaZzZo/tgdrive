import asyncio
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import make_settings
from telethon.tl.types import InputFileBig

from app.infrastructure import storage, telegram_upload
from app.infrastructure.storage import TelegramStorage
from app.infrastructure.telegram_upload import UPLOAD_PART_BYTES, upload_parallel


def fake_client(call):
    return SimpleNamespace(
        session=SimpleNamespace(auth_key=object(), server_address="dc", port=443, dc_id=2),
        _log={},
        _timeout=17,
        _proxy=("socks5", "example.test", 1080),
        _local_addr=None,
        _connection=lambda *args, **kwargs: (args, kwargs),
        _call=call,
        upload_file=AsyncMock(),
        send_file=AsyncMock(return_value=SimpleNamespace(id=101)),
    )


@pytest.mark.parametrize("reject_part", [False, True])
def test_four_connections_preserve_bytes_and_close_senders(workdir, monkeypatch, reject_part):
    async def run():
        path = workdir / "video.mp4"
        data = b"".join(bytes([i]) * UPLOAD_PART_BYTES for i in range(4)) + b"tail"
        path.write_bytes(data)
        first_requests, senders, progress = [], [], []
        saved = {}
        workers_started = asyncio.Event()

        def make_sender(*args, **kwargs):
            assert kwargs["connect_timeout"] == client._timeout
            sender = SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock())
            senders.append(sender)
            return sender

        async def save(sender, request):
            assert request.file_total_parts == 5
            if request.file_part < 4:
                first_requests.append(sender)
                assert not progress
                if len(first_requests) == 4:
                    workers_started.set()
                await workers_started.wait()
            if reject_part:
                return False
            saved[request.file_part] = request.bytes
            return True

        async def on_progress(sent, total):
            # A callback may suspend; updates must still complete in order.
            await asyncio.sleep(0)
            assert total == len(data)
            progress.append(sent)

        client = fake_client(save)
        monkeypatch.setattr(telegram_upload, "MTProtoSender", make_sender)
        monkeypatch.setattr(storage, "PARALLEL_UPLOAD_MIN_BYTES", 1)
        target = TelegramStorage(
            make_settings(workdir, tg_upload_connections=4, tg_segment_size=len(data))
        )
        target.client, target.chat, target._started = client, -100123, True
        upload = target.upload_file(path, path.name, progress=on_progress)
        if reject_part:
            with pytest.raises(OSError, match="Telegram 未保存文件分片"):
                await asyncio.wait_for(upload, timeout=2)
            client.send_file.assert_not_awaited()
            assert not progress
        else:
            assert await asyncio.wait_for(upload, timeout=2) == [
                {"part_index": 0, "tg_message_id": 101, "size": len(data)}
            ]
            assert b"".join(saved[i] for i in range(5)) == data
            assert progress == sorted(progress)
            assert progress[-1] == len(data)
            uploaded = client.send_file.await_args.args[1]
            assert (uploaded.name, uploaded.parts) == (path.name, 5)
            assert isinstance(uploaded, InputFileBig)
        client.upload_file.assert_not_awaited()
        assert len(senders) == 4
        assert {id(sender) for sender in first_requests} == {id(sender) for sender in senders}
        for sender in senders:
            assert sender.connect.await_args.args[0][1]["proxy"] == client._proxy
            sender.disconnect.assert_awaited_once()

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["connect", "rpc", "eof", "progress", "cancel"])
def test_parallel_failure_cancels_workers_and_cleans_up(monkeypatch, failure, caplog):
    async def run():
        senders, running = [], set()
        started = asyncio.Event()

        def make_sender(*args, **kwargs):
            sender = SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock())
            senders.append(sender)
            if failure == "connect" and len(senders) == 2:
                sender.connect.side_effect = OSError("connect failed")
            # Cleanup failure must not replace the original exception.
            if len(senders) == 1:
                sender.disconnect.side_effect = RuntimeError("disconnect failed")
            return sender

        async def save(sender, request):
            running.add(id(sender))
            try:
                started.set()
                if failure == "rpc":
                    raise OSError("rpc failed")
                if failure == "progress":
                    return True
                await asyncio.Event().wait()
            finally:
                running.remove(id(sender))

        async def progress(*args):
            raise OSError("progress failed")

        monkeypatch.setattr(telegram_upload, "MTProtoSender", make_sender)
        size = 4 * UPLOAD_PART_BYTES
        reader = io.BytesIO(b"" if failure == "eof" else b"x" * size)
        task = asyncio.create_task(
            upload_parallel(fake_client(save), reader, size, "x", 4, progress)
        )
        if failure == "cancel":
            await asyncio.wait_for(started.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(OSError):
                await asyncio.wait_for(task, timeout=2)
        assert not running
        assert len(senders) == (2 if failure == "connect" else 4)
        for sender in senders:
            sender.disconnect.assert_awaited_once()
        assert "Failed to disconnect" in caplog.text

    asyncio.run(run())


@pytest.mark.parametrize("connections,size", [(4, 100), (1, 32 * 1024 * 1024)])
def test_standard_upload_for_small_files_or_serial_setting(workdir, monkeypatch, connections, size):
    async def run():
        path = workdir / "file"
        with path.open("wb") as output:
            output.truncate(size)
        client = fake_client(AsyncMock())
        parallel = AsyncMock()
        monkeypatch.setattr(storage, "upload_parallel", parallel)
        target = TelegramStorage(
            make_settings(workdir, tg_upload_connections=connections, tg_segment_size=size)
        )
        target.client, target.chat, target._started = client, -100123, True
        await target.upload_file(path, path.name)
        client.upload_file.assert_awaited_once()
        parallel.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("connections", [0, 9, -1])
def test_invalid_connection_setting(workdir, connections):
    with pytest.raises(RuntimeError, match="TG_UPLOAD_CONNECTIONS"):
        make_settings(workdir, tg_upload_connections=connections).validate()


def test_connection_default_and_environment(workdir, monkeypatch):
    monkeypatch.delenv("TG_UPLOAD_CONNECTIONS", raising=False)
    assert make_settings(workdir).tg_upload_connections == 4
    monkeypatch.setenv("TG_UPLOAD_CONNECTIONS", "1")
    assert make_settings(workdir).tg_upload_connections == 1


@pytest.mark.parametrize("connections,size", [(0, 1), (9, 1), (4, 0)])
def test_helper_rejects_invalid_arguments(connections, size):
    async def run():
        with pytest.raises(ValueError):
            await upload_parallel(
                fake_client(AsyncMock()), io.BytesIO(), size, "x", connections, AsyncMock()
            )

    asyncio.run(run())


def test_multiple_segments_keep_offsets_and_small_tail_serial(workdir, monkeypatch):
    async def run():
        size = 32 * 1024 * 1024
        path = workdir / "segmented.bin"
        data = b"x" * size + b"tail"
        path.write_bytes(data)
        client = fake_client(AsyncMock())
        progress = []

        async def parallel(client, reader, length, name, connections, callback):
            assert connections == 4
            assert reader.read() == data[:size]
            await callback(length, length)
            return InputFileBig(1, 64, name)

        async def serial(reader, **kwargs):
            assert reader.read() == b"tail"
            await kwargs["progress_callback"](4, 4)

        client.upload_file.side_effect = serial
        monkeypatch.setattr(storage, "upload_parallel", parallel)
        target = TelegramStorage(make_settings(workdir, tg_segment_size=size))
        target.client, target.chat, target._started = client, -100123, True
        assert target.status()["upload_connections"] == 4
        parts = await target.upload_file(path, path.name, progress=lambda n, _: progress.append(n))
        assert [part["size"] for part in parts] == [size, 4]
        assert progress == [size, len(data)]
        client.upload_file.assert_awaited_once()
        assert client.send_file.await_count == 2

    asyncio.run(run())
