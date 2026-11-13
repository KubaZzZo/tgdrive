from __future__ import annotations

import asyncio
import os
import time
from contextlib import aclosing
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_downloads import make_file

from app.services import media_cache
from app.services.media_cache import MediaSliceCache


async def collect(stream):
    async with aclosing(stream):
        return b"".join([chunk async for chunk in stream])


async def settle(cache):
    tasks = [fill.task for fill in cache.pending.values() if fill.task is not None]
    if tasks:
        await asyncio.gather(*tasks)


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
            result = await collect(
                cache.stream(workspace, "version", start, end, len(origin), fetch)
            )
            await settle(cache)
            return result

        assert await read(0, 8) == b"abcdefgh"
        assert calls == [(0, 4), (4, 8)]
        for entry in directory.glob("*.slice"):
            os.utime(entry, times=(time.time() - 1, time.time() - 1))
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
        await settle(cache)

        release.clear()
        async with aclosing(cache.stream("workspace", "different", 0, 4, 4, fetch)) as stream:
            assert await anext(stream) == b"ab"
        assert cache.pending
        release.set()
        await settle(cache)
        assert not cache.pending
        assert cache._path("workspace", "different", 0).exists()

        release.clear()
        async with aclosing(cache.stream("workspace", "shutdown", 0, 4, 4, fetch)) as stream:
            assert await anext(stream) == b"ab"
        await cache.close()
        assert not cache.pending
        assert not cache._path("workspace", "shutdown", 0).exists()
        assert not list(cache.directory.glob("*.part"))

    asyncio.run(run())


def test_tiny_probe_returns_before_fill_and_neighboring_ranges_share_the_read(workdir):
    async def run():
        cache = MediaSliceCache(workdir / "cache", 8192, 0, slice_bytes=4)
        release = asyncio.Event()
        calls = []

        async def fetch(start, end):
            calls.append((start, end))
            yield b"ab"
            await release.wait()
            yield b"cd"

        async def read(start, end):
            return await collect(cache.stream("workspace", "version", start, end, 4, fetch))

        try:
            assert await asyncio.wait_for(read(0, 2), 1) == b"ab"
            assert cache.pending and calls == [(0, 4)]
            neighbor = asyncio.create_task(read(1, 4))
            await asyncio.sleep(0.05)
            assert not neighbor.done() and calls == [(0, 4)]
            release.set()
            assert await asyncio.wait_for(neighbor, 1) == b"bcd"
            await settle(cache)
            assert await read(0, 2) == b"ab"
            assert calls == [(0, 4)]
            assert cache.coalesced == 1 and cache.coalesced_bytes == 3
            assert cache.overfetch_bytes == 2
        finally:
            await cache.close()

    asyncio.run(run())


def test_cache_expiry_and_failed_tail_do_not_publish_partial_blocks(workdir):
    async def run():
        cache = MediaSliceCache(workdir / "cache", 8192, 0, slice_bytes=4, ttl_seconds=60)
        calls = []

        async def fetch(start, end):
            calls.append((start, end))
            yield b"abcd"[start:end]

        async def read(etag="version", end=4, source=fetch):
            return await collect(cache.stream("workspace", etag, 0, end, 4, source))

        try:
            assert await read() == b"abcd"
            await settle(cache)
            path = cache._path("workspace", "version", 0)
            expired = time.time() - 61
            os.utime(path, times=(expired, expired))
            assert await read() == b"abcd"
            await settle(cache)
            assert calls == [(0, 4), (0, 4)]
            os.utime(path, times=(expired, expired))
            await asyncio.to_thread(cache._prune)
            assert not path.exists()

            async def failed_tail(start, end):
                yield b"ab"
                raise OSError("source interrupted")

            assert await read("failed", 2, failed_tail) == b"ab"
            await settle(cache)
            assert not cache._path("workspace", "failed", 0).exists()
            import pytest

            with pytest.raises(OSError, match="source interrupted"):
                await read("failed", 4, failed_tail)
        finally:
            await cache.close()

    asyncio.run(run())


def test_cache_queue_is_bounded_and_low_capacity_reads_stay_exact(workdir, monkeypatch):
    async def run():
        monkeypatch.setattr(media_cache, "MAX_PENDING_FILLS", 1)
        cache = MediaSliceCache(workdir / "cache", 8192, 0, slice_bytes=4)
        release = asyncio.Event()
        calls = []
        data = b"abcdefgh"

        async def fetch(start, end):
            calls.append((start, end))
            if start == 0:
                yield data[:2]
                await release.wait()
                yield data[2:end]
            else:
                yield data[start:end]

        try:
            assert await collect(cache.stream("w", "v", 0, 1, 8, fetch)) == b"a"
            assert await collect(cache.stream("w", "v", 5, 6, 8, fetch)) == b"f"
            assert calls == [(0, 4), (5, 6)] and len(cache.pending) == 1
        finally:
            await cache.close()

    asyncio.run(run())


def test_warm_video_cache_still_checks_share_revocation(authenticated_client, workdir):
    client, app, storage = authenticated_client
    item = make_file(app, storage)
    cache = app.state.media_cache = MediaSliceCache(workdir / "cache", 8192, 0, slice_bytes=4)
    share = client.post(f"/api/items/{item['id']}/share", json={}).json()
    url = f"/s/{share['token']}/download"
    assert client.get(url, headers={"Range": "bytes=0-1"}).content == b"ab"
    client.portal.call(settle, cache)
    storage.iter_download = AsyncMock(side_effect=AssertionError("warm range reached Telegram"))
    assert client.get(url, headers={"Range": "bytes=1-2"}).content == b"bc"
    client.request("DELETE", "/api/shares", json={"token": share["token"]})
    assert client.get(url, headers={"Range": "bytes=1-2"}).status_code == 404


def test_fill_cancelled_before_first_step_releases_lease(workdir):
    import pytest

    async def run():
        cache = MediaSliceCache(workdir / "cache", 8192, 0, slice_bytes=4)
        leases = 0

        async def fetch(start, end):
            raise AssertionError("Canceled fill must not reach the source")
            yield b""

        def retain():
            nonlocal leases
            leases += 1
            asyncio.get_running_loop().call_soon(
                lambda: next(iter(cache.pending.values())).task.cancel()
            )

            def release():
                nonlocal leases
                leases -= 1

            return release

        try:
            with pytest.raises(OSError, match="stopped"):
                await collect(cache.stream("w", "v", 0, 1, 4, fetch, retain=retain))
            assert not cache.pending and leases == 0
        finally:
            await cache.close()

    asyncio.run(run())


def test_cold_tail_reads_exactly_without_waiting_for_prefix_then_warms_cache(workdir):
    async def run():
        size = 2 * 1024**2
        start = size - 64 * 1024
        origin = bytes(range(256)) * (size // 256)
        cache = MediaSliceCache(workdir / "cache", 4 * 1024**2, 0)
        fill_started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def fetch(first, last):
            calls.append((first, last))
            if first == 0:
                fill_started.set()
                yield origin[: 512 * 1024]
                await release.wait()
                yield origin[512 * 1024 : last]
            else:
                yield origin[first:last]

        async def read():
            return await collect(cache.stream("w", "v", start, size, size, fetch))

        try:
            assert await asyncio.wait_for(read(), 1) == origin[start:]
            await asyncio.wait_for(fill_started.wait(), 1)
            assert calls == [(start, size), (0, size)]
            # A pending fill whose prefix has not reached this position must not block it.
            assert await asyncio.wait_for(read(), 1) == origin[start:]
            assert calls == [(start, size), (0, size), (start, size)]
            assert len(cache.pending) == 1
            release.set()
            await settle(cache)
            assert await read() == origin[start:]
            assert len(calls) == 3 and cache.hit_bytes == size - start
            assert cache.overfetch_bytes == size
        finally:
            await cache.close()

    asyncio.run(run())


def test_cancelled_cold_seek_closes_exact_read_without_starting_fill(workdir):
    async def run():
        cache = MediaSliceCache(workdir / "cache", 8192, 0, slice_bytes=4)
        calls = []
        closed = False

        async def fetch(start, end):
            nonlocal closed
            calls.append((start, end))
            try:
                yield b"d"
            finally:
                closed = True

        try:
            async with aclosing(cache.stream("w", "v", 3, 4, 4, fetch)) as stream:
                assert await anext(stream) == b"d"
            assert closed and calls == [(3, 4)] and not cache.pending
        finally:
            await cache.close()

    asyncio.run(run())
