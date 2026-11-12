from __future__ import annotations

import asyncio
import os
from contextlib import aclosing
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_downloads import make_file

from app.services import media_cache
from app.services.media_cache import MediaSliceCache


async def collect(stream):
    async with aclosing(stream):
        return b"".join([chunk async for chunk in stream])


def test_video_cache_preserves_range_auth_and_cdn_accounting(authenticated_client, workdir):
    client, app, storage = authenticated_client
    item = make_file(app, storage)
    url = f"/api/files/{item['id']}/download"
    cache = app.state.media_cache = MediaSliceCache(workdir / "cache", 8192, 0, slice_bytes=4)
    app.state.cdn.charge = AsyncMock()
    reads = []
    download = storage.iter_download

    async def fetch(message_id, offset, length, **kwargs):
        reads.append((message_id, offset, length))
        async for data in download(message_id, offset, length, **kwargs):
            yield data

    storage.iter_download = fetch
    headers = {"Range": "bytes=0-2"}
    first = client.get(url, headers=headers)
    assert first.status_code == 206 and first.content == b"abc"
    assert first.headers["content-range"] == "bytes 0-2/10"
    assert reads == [(201, 0, 4)]
    assert cache.snapshot()["overfetch_bytes"] == 1

    client.cookies.clear()
    assert client.get(url, headers=headers).status_code == 401
    assert client.post("/api/login", json={"password": "correct-horse-battery-staple"}).is_success
    assert client.head(url, headers=headers).status_code == 206
    assert client.get(url, headers={"Range": "bytes=1-3"}).content == b"bcd"
    assert reads == [(201, 0, 4)]
    assert sum(call.args[0] for call in app.state.cdn.charge.await_args_list) == 6
    assert client.get("/api/traffic").json()["media_cache"]["hit_bytes"] == 3

    assert client.get(url + "?download=1", headers=headers).content == b"abc"
    assert reads[-1] == (201, 0, 3)
    app.state.db.rename_item(item["id"], "renamed.mp4")
    updated = client.get(url, headers=headers)
    assert updated.headers["etag"] != first.headers["etag"]
    assert reads[-1] == (201, 0, 4)


def test_slice_eviction_workspace_isolation_and_low_disk(workdir, monkeypatch):
    async def run():
        directory = workdir / "cache"
        cache = MediaSliceCache(directory, 8192, 100, slice_bytes=4)
        free = 8192
        monkeypatch.setattr(media_cache.shutil, "disk_usage", lambda _: SimpleNamespace(free=free))
        origin = b"abcdefghijklmnop"
        calls = []

        async def fetch(start, end):
            calls.append((start, end))
            yield origin[start:end]

        async def read(start, end, workspace="a"):
            return await collect(cache.stream(workspace, "version", start, end, len(origin), fetch))

        assert await read(0, 8) == b"abcdefgh"
        assert calls == [(0, 4), (4, 8)]
        for entry in directory.glob("*.slice"):
            os.utime(entry, ns=(1, 1))
        assert await read(1, 3) == b"bc"  # The first slice becomes the newest.
        assert await read(8, 12) == b"ijkl"
        assert cache._path("a", "version", 0).exists()
        assert not cache._path("a", "version", 4).exists()
        assert sum(path.stat().st_size for path in directory.glob("*.slice")) == 8

        free = 4195
        calls.clear()
        assert await read(0, 3, workspace="b") == b"abc"
        assert calls == [(0, 3)]
        assert not cache._path("b", "version", 0).exists()
        assert await read(0, 3) == b"abc"
        assert calls == [(0, 3)]

    asyncio.run(run())


def test_concurrent_fill_and_cancelled_slice(workdir):
    async def run():
        cache = MediaSliceCache(workdir / "cache", 8192, 0, slice_bytes=4)
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def fetch(start, end):
            nonlocal calls
            calls += 1
            yield b"ab"
            started.set()
            await release.wait()
            yield b"cd"

        async def read():
            return await collect(cache.stream("workspace", "version", 0, 4, 4, fetch))

        async with asyncio.timeout(3):
            first = asyncio.create_task(read())
            await started.wait()
            second = asyncio.create_task(read())
            release.set()
            assert await asyncio.gather(first, second) == [b"abcd", b"abcd"]
        assert calls == 1

        async with aclosing(cache.stream("workspace", "different", 0, 4, 4, fetch)) as stream:
            assert await anext(stream) == b"ab"
        assert not cache.pending
        assert not cache._path("workspace", "different", 0).exists()
        assert not list(cache.directory.glob("*.part"))

    asyncio.run(run())
