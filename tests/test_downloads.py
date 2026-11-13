from __future__ import annotations

import asyncio
import random
from contextlib import aclosing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import make_settings

from app.api.routes import files
from app.storage import DOWNLOAD_BLOCK_SIZE, PREFETCH_DEPTH, PREFETCH_MIN_BYTES, TelegramStorage


def make_file(app, storage, sizes=(4, 6)):
    data = b"abcdefghij"
    offset = 0
    parts = []
    for index, size in enumerate(sizes):
        storage.files[201 + index] = data[offset : offset + size]
        parts.append({"part_index": index, "tg_message_id": 201 + index, "size": size})
        offset += size
    return app.state.db.create_file(
        "视频.mp4",
        None,
        len(data),
        "video/mp4",
        None,
        str(storage.chat),
        parts,
    )


def test_download_resume_across_parts_and_if_range(authenticated_client):
    client, app, storage = authenticated_client
    item = make_file(app, storage)
    url = f"/api/files/{item['id']}/download?download=1"
    head = client.head(url)
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["content-length"] == "10"
    assert "attachment;" in head.headers["content-disposition"]
    assert "filename*=UTF-8''" in head.headers["content-disposition"]
    etag = head.headers["etag"]
    partial = client.get(url, headers={"Range": "bytes=2-7", "If-Range": etag})
    assert partial.status_code == 206
    assert partial.content == b"cdefgh"
    assert partial.headers["content-range"] == "bytes 2-7/10"
    assert client.get(url, headers={"Range": "bytes=-3"}).content == b"hij"
    for stale in ('"stale"', "W/" + etag):
        response = client.get(url, headers={"Range": "bytes=2-7", "If-Range": stale})
        assert response.status_code == 200
        assert response.content == b"abcdefghij"
        assert "content-range" not in response.headers


def test_incomplete_part_mapping_rejected_before_streaming(authenticated_client):
    client, app, storage = authenticated_client
    item = make_file(app, storage, (4,))
    url = f"/api/files/{item['id']}/download"
    assert client.head(url).status_code == 503
    assert client.get(url).status_code == 503


def test_only_large_attachment_ranges_use_prefetch(authenticated_client):
    client, app, storage = authenticated_client
    size = PREFETCH_MIN_BYTES
    item = app.state.db.create_file(
        "large.mp4",
        None,
        size,
        "video/mp4",
        None,
        str(storage.chat),
        [{"part_index": 0, "tg_message_id": 201, "size": size}],
    )
    calls = []

    async def download(message_id, offset, length, *, prefetch=False):
        calls.append((message_id, offset, length, prefetch))
        yield b"x" * length

    storage.iter_download = download
    url = f"/api/files/{item['id']}/download"
    assert client.get(url).status_code == 200
    assert client.get(url + "?download=1", headers={"Range": "bytes=1-2"}).content == b"xx"
    response = client.get(url + "?download=1")
    assert response.status_code == 200 and len(response.content) == size
    assert calls == [(201, 0, size, False), (201, 1, 2, False), (201, 0, size, True)]


def test_prefetched_blocks_preserve_order_and_range(workdir):
    async def run():
        offset = 17
        length = PREFETCH_MIN_BYTES + 67
        data = random.Random(0).randbytes(offset + length)
        ahead = asyncio.Event()
        started, completed = [], []
        active = peak = 0
        storage = TelegramStorage(make_settings(workdir))
        original_message = object()
        storage._get_message = AsyncMock(side_effect=[original_message, object()])

        async def read(message_id, start, size, message):
            nonlocal active, peak
            assert message is original_message
            started.append(start)
            active += 1
            peak = max(peak, active)
            try:
                if start == DOWNLOAD_BLOCK_SIZE:
                    await ahead.wait()
                elif start == 2 * DOWNLOAD_BLOCK_SIZE:
                    ahead.set()
                completed.append(start)
                yield data[start : start + size]
            finally:
                active -= 1

        storage._iter_download_once = read
        async with aclosing(storage.iter_download(1, offset, length, prefetch=True)) as stream:
            first = await anext(stream)
            assert started == [offset]
            received = first + b"".join([chunk async for chunk in stream])
        assert received == data[offset:]
        assert completed.index(2 * DOWNLOAD_BLOCK_SIZE) < completed.index(DOWNLOAD_BLOCK_SIZE)
        assert 1 < peak <= PREFETCH_DEPTH
        assert storage._prefetch_downloads == active == 0
        storage._get_message.assert_awaited_once_with(1)

    asyncio.run(run())


def test_disconnect_during_send_closes_pending_prefetch(workdir):
    async def run():
        pending = 0
        started = asyncio.Event()
        storage = TelegramStorage(make_settings(workdir))
        storage._get_message = AsyncMock(return_value=object())

        async def read(message_id, offset, length, message):
            nonlocal pending
            if offset == 0:
                yield b"x" * length
                return
            pending += 1
            if pending == PREFETCH_DEPTH:
                started.set()
            try:
                await asyncio.Event().wait()
            finally:
                pending -= 1

        async def send(event):
            if event["type"] == "http.response.body":
                await asyncio.wait_for(started.wait(), 2)
                raise OSError("client disconnected")

        storage._iter_download_once = read
        response = files.FileStreamingResponse(
            storage.iter_download(
                1,
                0,
                max(PREFETCH_MIN_BYTES, (PREFETCH_DEPTH + 1) * DOWNLOAD_BLOCK_SIZE),
                prefetch=True,
            )
        )
        with pytest.raises(OSError, match="client disconnected"):
            await response.stream_response(send)
        assert storage._prefetch_downloads == pending == 0

    asyncio.run(run())


def test_authenticated_media_probe_preserves_range_and_correlates_timing(
    authenticated_client, monkeypatch
):
    client, app, storage = authenticated_client
    item = make_file(app, storage)
    url = f"/api/files/{item['id']}/download"
    calls = []
    monkeypatch.setattr(files.logger, "info", lambda *args, **kwargs: calls.append((args, kwargs)))
    response = client.get(url, headers={"Range": "bytes=2-7", "X-TGDrive-Media-Probe": "1"})
    assert response.status_code == 206 and response.content == b"cdefgh"
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[1] == "probe" and args[2] >= 0 and args[-2:] == (6, 6)
    assert kwargs["extra"]["request_id"] == response.headers["X-Request-ID"]
    assert client.get(url, headers={"Range": "bytes=2-7"}).status_code == 206
    assert len(calls) == 1


@pytest.mark.parametrize("failure", [ConnectionError, TimeoutError, OSError])
def test_interrupted_download_retries_from_delivered_offset(workdir, failure):
    async def run():
        data = bytes(range(256)) * 4096
        calls = []

        class Stream:
            def __init__(self, offset, chunk_size):
                self.offset, self.chunk_size = offset, chunk_size
                self.closed = False

            async def __aiter__(self):
                yield data[self.offset : self.offset + self.chunk_size]
                if len(calls) == 1:
                    raise failure("connection interrupted")
                yield data[self.offset + self.chunk_size : self.offset + 2 * self.chunk_size]

            async def close(self):
                self.closed = True

        def download(message, **kwargs):
            stream = Stream(kwargs["offset"], kwargs["chunk_size"])
            calls.append(stream)
            return stream

        storage = TelegramStorage(make_settings(workdir))
        storage.client = SimpleNamespace(iter_download=download)
        storage.chat, storage._started = -100123, True
        storage._get_message = AsyncMock(return_value=SimpleNamespace(document=True))
        received = b"".join([chunk async for chunk in storage.iter_download(1, 17, 800_000)])
        assert received == data[17:800_017]
        assert len(calls) == 2
        assert all(stream.closed for stream in calls)

    asyncio.run(run())


def test_download_retry_is_bounded(workdir):
    async def run():
        storage = TelegramStorage(make_settings(workdir))
        calls = 0

        async def fail(*args):
            nonlocal calls
            calls += 1
            raise ConnectionError("offline")
            yield  # make this an async generator

        storage._iter_download_once = fail
        with pytest.raises(ConnectionError):
            await anext(storage.iter_download(1, 0, 10))
        assert calls == 3

    asyncio.run(run())
