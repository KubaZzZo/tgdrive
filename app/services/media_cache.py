from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import sqlite3
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing, closing, suppress
from dataclasses import dataclass, field
from pathlib import Path

import anyio

MIB = 1024 * 1024
SLICE_BYTES = 2 * MIB
OUTPUT_CHUNK_BYTES = 256 * 1024
MAX_PENDING_FILLS = 8
CACHE_TTL_SECONDS = 600
FILL_TIMEOUT_SECONDS = 60
DISK_BLOCK_BYTES = 4096
CACHE_FILE = re.compile(r"[0-9a-f]{64}\.(?:slice|part)")

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
    """Private read-through disk cache for immutable, aligned byte slices."""

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
        self.pending: dict[Path, PendingSlice] = {}
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
        if self._maintenance:
            tasks.append(self._maintenance)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._maintenance = None

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

    def _path(self, namespace: str, etag: str, start: int) -> Path:
        key = hashlib.sha256(
            f"{namespace}\0{etag}\0{self.slice_bytes}\0{start}".encode()
        ).hexdigest()
        return self.directory / f"{key}.slice"

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
        try:
            # SQLite's reserved lock serializes the disk budget across app processes.
            with closing(sqlite3.connect(self.directory / ".writer.sqlite3", timeout=0.1)) as lock:
                lock.execute("BEGIN IMMEDIATE")
                entries = []
                used = 0
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
                    entries.append((stat.st_mtime_ns, entry, size))
                    used += size
                if not self._has_space(len(data)):
                    return
                for _, entry, size in sorted(entries):
                    if used + allocation <= self.max_bytes:
                        break
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
    ) -> AsyncGenerator[bytes, None]:
        cursor = start
        while cursor < end:
            slice_start = cursor // self.slice_bytes * self.slice_bytes
            slice_end = min(slice_start + self.slice_bytes, size)
            length = slice_end - slice_start
            requested_end = min(slice_end, end)
            offset = cursor - slice_start
            requested = requested_end - cursor
            path = self._path(namespace, etag, slice_start)
            cached = await anyio.to_thread.run_sync(self._read, path, length, offset, requested)
            if cached is not None:
                for chunk in self._hit_chunks(cached):
                    yield chunk
            else:
                self.misses += 1
                pending = self.pending.get(path)
                if offset and (pending is None or len(pending.data) <= offset):
                    # A cold seek must not wait for the slice prefix. Start its fill only
                    # after the requested bytes reached the caller, without competing.
                    async with aclosing(fetch(cursor, requested_end)) as source:
                        async for chunk in source:
                            yield chunk
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
                    self.coalesced += int(coalesced)
                    async with aclosing(
                        self._follow(pending, offset, offset + requested)
                    ) as source:
                        async for chunk in source:
                            if coalesced:
                                self.coalesced_bytes += len(chunk)
                            yield chunk
                else:
                    # Low disk or a full queue: preserve the requested upstream range.
                    requested_end = end
                    async with aclosing(fetch(cursor, requested_end)) as source:
                        async for chunk in source:
                            yield chunk
            cursor = requested_end

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
                self.misses -= 1
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
