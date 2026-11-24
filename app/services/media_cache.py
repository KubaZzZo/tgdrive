from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import sqlite3
import time
from collections import OrderedDict
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing, closing, suppress
from dataclasses import dataclass, field
from pathlib import Path

import anyio

from app.services.video_prepare import find_tail_mp4_index

MIB = 1024 * 1024
SLICE_BYTES = 2 * MIB
OUTPUT_CHUNK_BYTES = 256 * 1024
MAX_PENDING_FILLS = 8
MAX_INDEX_WARMERS = 2
MAX_STARTUP_CACHE_BYTES = 128 * MIB
MAX_INDEX_LAYOUTS = 256
CACHE_TTL_SECONDS = 600
FILL_TIMEOUT_SECONDS = 60
DISK_BLOCK_BYTES = 4096
CACHE_FILE = re.compile(r"[0-9a-f]{64}\.(?:slice|startup|part)")

FetchRange = Callable[[int, int], AsyncGenerator[bytes, None]]


def allocated_bytes(size: int) -> int:
    return (size + DISK_BLOCK_BYTES - 1) // DISK_BLOCK_BYTES * DISK_BLOCK_BYTES


@dataclass
class PendingSlice:
    data: bytearray = field(default_factory=bytearray)
    changed: anyio.Event = field(default_factory=anyio.Event)
    done: bool = False
    error: Exception | None = None
    task: asyncio.Task[None] | None = None

    def notify(self) -> None:
        changed, self.changed = self.changed, anyio.Event()
        changed.set()


class MediaSliceCache:
    """Private read-through disk cache for immutable byte slices."""

    def __init__(
        self,
        directory: Path,
        max_bytes: int,
        min_free_bytes: int,
        slice_bytes: int = SLICE_BYTES,
        ttl_seconds: int = CACHE_TTL_SECONDS,
    ):
        self.directory = directory
        self.max_bytes = max_bytes
        self.min_free_bytes = min_free_bytes
        self.slice_bytes = slice_bytes
        self.ttl_seconds = ttl_seconds
        self.startup_max_bytes = min(max_bytes // 4, MAX_STARTUP_CACHE_BYTES)
        self.pending: dict[Path, PendingSlice] = {}
        self._warmers: dict[Path, asyncio.Task[None]] = {}
        self._indexes: OrderedDict[Path, tuple[float, tuple[int, int] | None]] = OrderedDict()
        self._maintenance: asyncio.Task[None] | None = None
        self._closed = False
        self.hits = 0
        self.misses = 0
        self.hit_bytes = 0
        self.overfetch_bytes = 0
        self.coalesced = 0
        self.coalesced_bytes = 0

    @property
    def enabled(self) -> bool:
        return self.max_bytes > 0

    def snapshot(self) -> dict[str, int | bool]:
        return {
            "enabled": self.enabled,
            "max_bytes": self.max_bytes,
            "min_free_bytes": self.min_free_bytes,
            "slice_bytes": self.slice_bytes,
            "startup_max_bytes": self.startup_max_bytes,
            "ttl_seconds": self.ttl_seconds,
            "pending": len(self.pending),
            "hits": self.hits,
            "misses": self.misses,
            "hit_bytes": self.hit_bytes,
            "overfetch_bytes": self.overfetch_bytes,
            "coalesced": self.coalesced,
            "coalesced_bytes": self.coalesced_bytes,
        }

    def start(self) -> None:
        if self.enabled and self._maintenance is None and not self._closed:
            self._maintenance = asyncio.create_task(self._maintain(), name="media-cache-cleanup")

    async def close(self) -> None:
        self._closed = True
        tasks = [fill.task for fill in self.pending.values() if fill.task is not None]
        tasks.extend(self._warmers.values())
        if self._maintenance:
            tasks.append(self._maintenance)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._maintenance = None

    def warm_index(
        self,
        namespace: str,
        etag: str,
        size: int,
        fetch: FetchRange,
        retain: Callable[[], Callable[[], None]],
    ) -> None:
        key = self._path(namespace, etag, 0)
        self._index_range(key)
        if (
            self._closed
            or self.startup_max_bytes < allocated_bytes(self.slice_bytes)
            or len(self._warmers) >= MAX_INDEX_WARMERS
            or key in self._warmers
            or key in self._indexes
        ):
            return
        release = retain()

        async def read(first: int, last: int) -> bytes:
            # Share the header with the player. Other headers are small probes at
            # box boundaries and should not start fills of the surrounding slices.
            source = (
                self.stream(
                    namespace,
                    etag,
                    first,
                    last,
                    size,
                    fetch,
                    retain=retain,
                    mp4=True,
                    cache_only=True,
                )
                if first == 0
                else fetch(first, last)
            )
            async with aclosing(source):
                data = b"".join([chunk async for chunk in source])
            if len(data) != last - first:
                raise OSError("MP4 header data is unavailable")
            return data

        async def run() -> None:
            with suppress(Exception):
                async with asyncio.timeout(FILL_TIMEOUT_SECONDS):
                    index = await find_tail_mp4_index(size, read)
                    if index:
                        first, last = max(index[0], self.slice_bytes), index[1]
                        available = self.startup_max_bytes - allocated_bytes(self.slice_bytes)
                        index = (
                            (first, last)
                            if first < last and allocated_bytes(last - first) <= available
                            else None
                        )
                    self._indexes[key] = (time.monotonic(), index)
                    if len(self._indexes) > MAX_INDEX_LAYOUTS:
                        self._indexes.popitem(last=False)
                    if index:
                        first, last = index
                        async with aclosing(
                            self.stream(
                                namespace,
                                etag,
                                first,
                                last,
                                size,
                                fetch,
                                retain=retain,
                                mp4=True,
                                cache_only=True,
                            )
                        ) as source:
                            async for _chunk in source:
                                pass

        task = self._warmers[key] = asyncio.create_task(run(), name="media-index-warm")

        def done(_task: asyncio.Task[None]) -> None:
            self._warmers.pop(key, None)
            release()

        task.add_done_callback(done)

    def _index_range(self, key: Path) -> tuple[int, int] | None:
        entry = self._indexes.get(key)
        if entry is None:
            return None
        if time.monotonic() - entry[0] >= self.ttl_seconds:
            self._indexes.pop(key)
            return None
        return entry[1]

    async def _maintain(self) -> None:
        while True:
            await anyio.to_thread.run_sync(self._prune)
            await asyncio.sleep(min(60, self.ttl_seconds))

    def _prune(self) -> None:
        if not self.directory.is_dir():
            return
        try:
            with closing(sqlite3.connect(self.directory / ".writer.sqlite3", timeout=0.1)) as lock:
                lock.execute("BEGIN IMMEDIATE")
                cutoff = time.time() - self.ttl_seconds
                for path in self.directory.iterdir():
                    if (
                        CACHE_FILE.fullmatch(path.name)
                        and not path.is_symlink()
                        and path.stat().st_mtime < cutoff
                    ):
                        path.unlink()
        except (OSError, sqlite3.Error):
            pass

    def _path(self, namespace: str, etag: str, start: int, startup_end: int | None = None) -> Path:
        suffix = ".slice"
        partition = ""
        if startup_end is not None:
            suffix = ".startup"
            partition = f"\0{startup_end}"
        key = hashlib.sha256(
            f"{namespace}\0{etag}\0{self.slice_bytes}\0{start}{partition}".encode()
        ).hexdigest()
        return self.directory / (key + suffix)

    def _read(self, path: Path, size: int, start: int, length: int) -> bytes | None:
        try:
            with path.open("rb") as file:
                stat = os.fstat(file.fileno())
                if stat.st_size != size or stat.st_mtime < time.time() - self.ttl_seconds:
                    return None
                file.seek(start)
                data = file.read(length)
            if len(data) != length:
                return None
            with suppress(OSError):
                os.utime(path)
            return data
        except OSError:
            return None

    def _has_space(self, size: int) -> bool:
        size = allocated_bytes(size)
        if size > self.max_bytes:
            return False
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            return shutil.disk_usage(self.directory).free >= self.min_free_bytes + size
        except OSError:
            return False

    def _store(self, path: Path, data: bytearray) -> None:
        temporary = path.with_suffix(".part")
        allocation = allocated_bytes(len(data))
        startup = path.suffix == ".startup"
        if startup and allocation > self.startup_max_bytes:
            return
        try:
            # SQLite's reserved lock serializes the disk budget across app processes.
            with closing(sqlite3.connect(self.directory / ".writer.sqlite3", timeout=0.1)) as lock:
                lock.execute("BEGIN IMMEDIATE")
                entries = []
                used = 0
                startup_used = 0
                for entry in self.directory.iterdir():
                    if not CACHE_FILE.fullmatch(entry.name) or entry.is_symlink():
                        continue
                    if entry.suffix == ".part" or entry == path:
                        entry.unlink()
                        continue
                    stat = entry.stat()
                    if stat.st_mtime < time.time() - self.ttl_seconds:
                        entry.unlink()
                        continue
                    size = max(allocated_bytes(stat.st_size), getattr(stat, "st_blocks", 0) * 512)
                    is_startup = entry.suffix == ".startup"
                    entries.append((stat.st_mtime_ns, entry, size, is_startup))
                    used += size
                    if is_startup:
                        startup_used += size
                if not self._has_space(len(data)):
                    return
                entries.sort()
                startup_limit = self.startup_max_bytes - (allocation if startup else 0)
                for _, entry, size, is_startup in entries:
                    if startup_used <= startup_limit:
                        break
                    if is_startup:
                        entry.unlink()
                        startup_used -= size
                        used -= size
                if startup_used + allocation > self.max_bytes:
                    return
                for _, entry, size, is_startup in entries:
                    if used + allocation <= self.max_bytes:
                        break
                    if not is_startup:
                        entry.unlink()
                        used -= size
                try:
                    with temporary.open("xb") as file:
                        file.write(data)
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
        except (OSError, sqlite3.Error):
            # Disk errors or lock contention must not interrupt playback.
            return

    async def stream(
        self,
        namespace: str,
        etag: str,
        start: int,
        end: int,
        size: int,
        fetch: FetchRange,
        *,
        retain: Callable[[], Callable[[], None]] | None = None,
        read_ahead: bool = False,
        mp4: bool = False,
        cache_only: bool = False,
        mark_source: Callable[[str], None] | None = None,
    ) -> AsyncGenerator[bytes, None]:
        startup_cache = mp4 and self.startup_max_bytes >= allocated_bytes(self.slice_bytes)
        layout_key = self._path(namespace, etag, 0) if startup_cache else None
        cursor = start
        while cursor < end:
            slice_start = cursor // self.slice_bytes * self.slice_bytes
            slice_end = min(slice_start + self.slice_bytes, size)
            index = self._index_range(layout_key) if layout_key is not None else None
            in_index = index is not None and index[0] <= cursor < index[1]
            if index and in_index:
                # Anchor the cache at moov, so no player waits for the unrelated mdat prefix.
                slice_start = index[0] + (cursor - index[0]) // self.slice_bytes * self.slice_bytes
                slice_end = min(slice_start + self.slice_bytes, index[1])
            startup = startup_cache and (in_index or slice_start == 0)
            length = slice_end - slice_start
            requested_end = min(slice_end, end)
            if index and cursor < index[0] < requested_end:
                requested_end = index[0]
            offset = cursor - slice_start
            requested = requested_end - cursor
            path = self._path(namespace, etag, slice_start, slice_end if startup else None)
            cached = await anyio.to_thread.run_sync(self._read, path, length, offset, requested)
            if cached is not None:
                if mark_source:
                    mark_source("cache")
                for chunk in self._hit_chunks(cached):
                    yield chunk
            else:
                self.misses += 1
                pending = self.pending.get(path)
                crosses_index = index is not None and slice_start < index[0] < slice_end
                arrived = len(pending.data) if pending is not None else 0
                # A short MP4 header probe can share an active index fill. A
                # distant seek should still go directly to the requested offset.
                can_share_index = in_index and offset - arrived < OUTPUT_CHUNK_BYTES
                if (offset or crosses_index) and offset >= arrived and not can_share_index:
                    if cache_only:
                        return
                    # A cold seek must not wait for the slice prefix. Start its fill only
                    # after the requested bytes reached the caller, without competing.
                    if mark_source:
                        mark_source("telegram-exact")
                    async with aclosing(fetch(cursor, requested_end)) as source:
                        async for chunk in source:
                            yield chunk
                    # Leave this boundary to the exact moov fill when the
                    # normal slice would overlap the index.
                    if not crosses_index and not in_index:
                        await self._get_or_start_fill(
                            path, slice_start, slice_end, slice_start, slice_start, fetch, retain
                        )
                    cursor = requested_end
                    continue
                pending, created = await self._get_or_start_fill(
                    path, slice_start, slice_end, cursor, requested_end, fetch, retain
                )
                coalesced = pending is not None and not created
                if pending is not None:
                    if mark_source:
                        mark_source("shared-fill" if coalesced else "telegram-fill")
                    self.coalesced += int(coalesced)
                    prefetch_next = (read_ahead or in_index) and requested_end == slice_end < end
                    async with aclosing(
                        self._follow(pending, offset, offset + requested)
                    ) as source:
                        async for chunk in source:
                            if coalesced:
                                self.coalesced_bytes += len(chunk)
                            yield chunk
                            if prefetch_next:
                                prefetch_next = False
                                await self._read_ahead(
                                    namespace, etag, slice_end, end, size, fetch, retain, index
                                )
                elif cache_only:
                    # Speculation stops when a slice cannot be stored. The next
                    # real request can still stream directly from the source.
                    return
                else:
                    # Low disk or a full queue: preserve the requested upstream range.
                    requested_end = end
                    if mark_source:
                        mark_source("telegram-direct")
                    async with aclosing(fetch(cursor, requested_end)) as source:
                        async for chunk in source:
                            yield chunk
            cursor = requested_end

    async def _read_ahead(
        self,
        namespace: str,
        etag: str,
        start: int,
        end: int,
        size: int,
        fetch: FetchRange,
        retain: Callable[[], Callable[[], None]] | None,
        index: tuple[int, int] | None,
    ) -> None:
        in_index = index is not None and index[0] <= start < index[1]
        limit = MAX_PENDING_FILLS if in_index else MAX_PENDING_FILLS // 2
        if len(self.pending) >= limit:
            return
        if index and in_index:
            stop = min(start + self.slice_bytes, index[1])
        elif start % self.slice_bytes:
            return
        else:
            stop = min(start + self.slice_bytes, size)
            if index and start < index[0] < stop:
                return
        path = self._path(namespace, etag, start, stop if in_index else None)
        if await anyio.to_thread.run_sync(self._read, path, stop - start, 0, 1) is None:
            await self._get_or_start_fill(path, start, stop, start, min(stop, end), fetch, retain)

    async def _get_or_start_fill(
        self,
        path: Path,
        start: int,
        end: int,
        wanted_start: int,
        wanted_end: int,
        fetch: FetchRange,
        retain: Callable[[], Callable[[], None]] | None,
    ) -> tuple[PendingSlice | None, bool]:
        if (pending := self.pending.get(path)) is not None:
            return pending, False
        if self._closed or len(self.pending) >= MAX_PENDING_FILLS:
            return None, False
        if not await anyio.to_thread.run_sync(self._has_space, end - start):
            return None, False
        # Recheck after the disk check: another request may have started a fill.
        if (pending := self.pending.get(path)) is not None:
            return pending, False
        if self._closed or len(self.pending) >= MAX_PENDING_FILLS:
            return None, False
        pending = self.pending[path] = PendingSlice()
        release = retain() if retain else None
        pending.task = asyncio.create_task(
            self._fill(path, pending, start, end, wanted_start, wanted_end, fetch),
            name="media-cache-fill",
        )
        pending.task.add_done_callback(lambda task: self._finish(path, pending, release, task))
        return pending, True

    def _hit_chunks(self, cached: bytes):
        self.hits += 1
        for index in range(0, len(cached), OUTPUT_CHUNK_BYTES):
            chunk = cached[index : index + OUTPUT_CHUNK_BYTES]
            self.hit_bytes += len(chunk)
            yield chunk

    async def _follow(
        self, pending: PendingSlice, start: int, end: int
    ) -> AsyncGenerator[bytes, None]:
        cursor = start
        while cursor < end:
            available = min(len(pending.data), end, cursor + OUTPUT_CHUNK_BYTES)
            if available > cursor:
                yield bytes(pending.data[cursor:available])
                cursor = available
            elif pending.done:
                raise pending.error or OSError("Media source returned an incomplete slice")
            else:
                await pending.changed.wait()

    async def _fill(
        self,
        path: Path,
        pending: PendingSlice,
        start: int,
        end: int,
        wanted_start: int,
        wanted_end: int,
        fetch: FetchRange,
    ) -> None:
        position = start
        try:
            # A fill may have completed while this request checked disk space.
            cached = await anyio.to_thread.run_sync(self._read, path, end - start, 0, end - start)
            if cached is not None:
                pending.data.extend(cached)
                pending.notify()
                return
            async with asyncio.timeout(FILL_TIMEOUT_SECONDS), aclosing(fetch(start, end)) as source:
                async for chunk in source:
                    next_position = position + len(chunk)
                    if next_position > end:
                        raise OSError("Media source exceeded the requested slice")
                    pending.data.extend(chunk)
                    overlap = max(0, min(next_position, wanted_end) - max(position, wanted_start))
                    self.overfetch_bytes += len(chunk) - overlap
                    position = next_position
                    pending.notify()
                if position != end:
                    raise OSError("Media source returned an incomplete slice")
                await anyio.to_thread.run_sync(self._store, path, pending.data)
        except asyncio.CancelledError:
            pending.error = OSError("Media cache fill was stopped")
            raise
        except Exception as exc:
            pending.error = exc

    def _finish(
        self,
        path: Path,
        pending: PendingSlice,
        release: Callable[[], None] | None,
        task: asyncio.Task[None],
    ) -> None:
        # A task canceled before its first step still needs to release its channel lease.
        if task.cancelled() and pending.error is None:
            pending.error = OSError("Media cache fill was stopped")
        if self.pending.get(path) is pending:
            del self.pending[path]
        pending.done = True
        pending.notify()
        if release:
            release()
