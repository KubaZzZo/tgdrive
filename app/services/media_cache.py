from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing, closing, suppress
from pathlib import Path

import anyio

MIB = 1024 * 1024
SLICE_BYTES = 4 * MIB
OUTPUT_CHUNK_BYTES = 256 * 1024
MAX_PENDING_FILLS = 8
DISK_BLOCK_BYTES = 4096
CACHE_FILE = re.compile(r"[0-9a-f]{64}\.(?:slice|part)")

FetchRange = Callable[[int, int], AsyncGenerator[bytes, None]]


def allocated_bytes(size: int) -> int:
    return (size + DISK_BLOCK_BYTES - 1) // DISK_BLOCK_BYTES * DISK_BLOCK_BYTES


class MediaSliceCache:
    """Private read-through disk cache for immutable, aligned byte slices."""

    def __init__(
        self, directory: Path, max_bytes: int, min_free_bytes: int, slice_bytes: int = SLICE_BYTES
    ):
        self.directory = directory
        self.max_bytes = max_bytes
        self.min_free_bytes = min_free_bytes
        self.slice_bytes = slice_bytes
        self.pending: dict[Path, anyio.Event] = {}
        self.hits = 0
        self.misses = 0
        self.hit_bytes = 0
        self.overfetch_bytes = 0

    @property
    def enabled(self) -> bool:
        return self.max_bytes > 0

    def snapshot(self) -> dict[str, int | bool]:
        return {
            "enabled": self.enabled,
            "max_bytes": self.max_bytes,
            "min_free_bytes": self.min_free_bytes,
            "slice_bytes": self.slice_bytes,
            "hits": self.hits,
            "misses": self.misses,
            "hit_bytes": self.hit_bytes,
            "overfetch_bytes": self.overfetch_bytes,
        }

    def _path(self, namespace: str, etag: str, start: int) -> Path:
        key = hashlib.sha256(
            f"{namespace}\0{etag}\0{self.slice_bytes}\0{start}".encode()
        ).hexdigest()
        return self.directory / f"{key}.slice"

    def _read(self, path: Path, size: int, start: int, length: int) -> bytes | None:
        try:
            with path.open("rb") as file:
                if os.fstat(file.fileno()).st_size != size:
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
        self, namespace: str, etag: str, start: int, end: int, size: int, fetch: FetchRange
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
            # Keep tiny probes and deep seeks exact. Substantial aligned reads can
            # complete the slice with no more extra bytes than the client requested.
            fill = requested * 2 >= length and offset == 0
            cached = await anyio.to_thread.run_sync(self._read, path, length, offset, requested)
            if cached is None and fill and (pending := self.pending.get(path)) is not None:
                with anyio.move_on_after(1) as wait:
                    await pending.wait()
                if not wait.cancel_called:
                    cached = await anyio.to_thread.run_sync(
                        self._read, path, length, offset, requested
                    )
            if cached is not None:
                for chunk in self._hit_chunks(cached):
                    yield chunk
            else:
                self.misses += 1
                if fill and await anyio.to_thread.run_sync(self._has_space, length):
                    if path not in self.pending and len(self.pending) < MAX_PENDING_FILLS:
                        async with aclosing(
                            self._fill(path, slice_start, slice_end, requested_end, fetch)
                        ) as source:
                            async for chunk in source:
                                yield chunk
                        cursor = requested_end
                        continue
                elif fill:
                    requested_end = end  # Low disk: keep the original upstream read size.
                async with aclosing(fetch(cursor, requested_end)) as source:
                    async for chunk in source:
                        yield chunk
            cursor = requested_end

    def _hit_chunks(self, cached: bytes):
        self.hits += 1
        for index in range(0, len(cached), OUTPUT_CHUNK_BYTES):
            chunk = cached[index : index + OUTPUT_CHUNK_BYTES]
            self.hit_bytes += len(chunk)
            yield chunk

    async def _fill(
        self, path: Path, start: int, end: int, wanted_end: int, fetch: FetchRange
    ) -> AsyncGenerator[bytes, None]:
        done = self.pending[path] = anyio.Event()
        data = bytearray()
        position = start
        try:
            # A fill may have completed while this request checked disk space.
            cached = await anyio.to_thread.run_sync(
                self._read, path, end - start, 0, wanted_end - start
            )
            if cached is not None:
                self.misses -= 1
                for chunk in self._hit_chunks(cached):
                    yield chunk
                return
            async with aclosing(fetch(start, end)) as source:
                async for chunk in source:
                    next_position = position + len(chunk)
                    if next_position > end:
                        raise OSError("Media source exceeded the requested slice")
                    data.extend(chunk)
                    overlap = max(0, min(next_position, wanted_end) - position)
                    self.overfetch_bytes += len(chunk) - overlap
                    position = next_position
                    if overlap:
                        yield chunk[:overlap]
            if position == end:
                await anyio.to_thread.run_sync(self._store, path, data)
        except Exception:
            # A failure in the optional tail cannot invalidate bytes already served.
            if position < wanted_end:
                raise
        finally:
            del self.pending[path]
            done.set()
