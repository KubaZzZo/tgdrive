from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import anyio

from app.services.upload_temp import faststart_path

if TYPE_CHECKING:
    from app.services.uploads import UploadSpace

logger = logging.getLogger("tgdrive.video_prepare")
REMUX_SLACK_BYTES = 8 * 1024 * 1024
MP4_WARM_MAX_BOXES = 16
MP4_WARM_MAX_BYTES = 16 * 1024 * 1024
MP4_HEADER_PROBE_BYTES = 16 * 1024


async def find_tail_mp4_index(
    size: int, read: Callable[[int, int], Awaitable[bytes]]
) -> tuple[int, int] | None:
    """Locate a bounded tail index without reading the intervening media payload."""
    prefix = await read(0, min(size, MP4_HEADER_PROBE_BYTES))
    offset = 0
    saw_media = False
    for _ in range(MP4_WARM_MAX_BOXES):
        if offset + 8 > size:
            return None
        end = min(offset + 16, size)
        header = prefix[offset:end] if end <= len(prefix) else await read(offset, end)
        if len(header) < 8:
            return None
        length = int.from_bytes(header[:4], "big")
        kind = header[4:8]
        header_size = 8
        if length == 1:
            if len(header) < 16:
                return None
            length = int.from_bytes(header[8:16], "big")
            header_size = 16
        elif length == 0:
            length = size - offset
        if length < header_size or offset + length > size or (offset == 0 and kind != b"ftyp"):
            return None
        if kind == b"moov":
            return (offset, offset + length) if saw_media and length <= MP4_WARM_MAX_BYTES else None
        if kind == b"moof":
            return None
        saw_media |= kind == b"mdat"
        offset += length
    return None


def mp4_layout(path: Path) -> str | None:
    """Inspect only top-level box headers; fragmented/invalid MP4 remains untouched."""
    size = path.stat().st_size
    found: dict[bytes, int] = {}
    with path.open("rb") as source:
        offset = 0
        for _ in range(128):
            if offset == size:
                break
            source.seek(offset)
            header = source.read(8)
            if len(header) != 8:
                return None
            length, kind = int.from_bytes(header[:4], "big"), header[4:]
            header_size = 8
            if length == 1:
                extended = source.read(8)
                if len(extended) != 8:
                    return None
                length, header_size = int.from_bytes(extended, "big"), 16
            elif length == 0:
                length = size - offset
            if length < header_size or offset + length > size or kind == b"moof":
                return None
            if kind == b"moov" and length > 64 * 1024 * 1024:
                return None
            found.setdefault(kind, offset)
            offset += length
        if offset != size or not {b"ftyp", b"moov", b"mdat"} <= found.keys():
            return None
    return "faststart" if found[b"moov"] < found[b"mdat"] else "tail"


async def remux_faststart(source: Path, target: Path, max_bytes: int, timeout: int) -> bool:
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-protocol_whitelist",
        "file,pipe",
        "-max_alloc",
        str(64 * 1024 * 1024),
        "-threads",
        "1",
        "-enable_drefs",
        "0",
        "-i",
        str(source.resolve()),
        "-map",
        "0",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        "-fs",
        str(max_bytes),
        "-y",
        "-f",
        "mp4",
        str(target.resolve()),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    wait = asyncio.create_task(process.wait())
    try:
        async with asyncio.timeout(timeout):
            code = await asyncio.shield(wait)
        return (
            code == 0
            and 0 < target.stat().st_size < max_bytes
            and await asyncio.to_thread(mp4_layout, target) == "faststart"
        )
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        with anyio.CancelScope(shield=True):
            await asyncio.shield(wait)


@asynccontextmanager
async def prepared_video(
    path: Path,
    name: str,
    space: UploadSpace,
    *,
    enabled: bool,
    timeout: int,
    slots: asyncio.Semaphore | None = None,
) -> AsyncIterator[Path]:
    if not enabled or Path(name).suffix.lower() != ".mp4":
        yield path
        return
    if await asyncio.to_thread(mp4_layout, path) != "tail":
        yield path
        return
    from app.services.uploads import UploadSpaceError

    target = faststart_path(path)
    limit = path.stat().st_size + REMUX_SLACK_BYTES
    async with AsyncExitStack() as stack:
        try:
            await stack.enter_async_context(space.reserve(target, limit))
        except UploadSpaceError:
            logger.info("MP4 faststart skipped: insufficient temporary disk budget")
            yield path
            return
        try:
            optimized = False
            try:
                if slots is None:
                    optimized = await remux_faststart(path, target, limit, timeout)
                else:
                    async with slots:
                        optimized = await remux_faststart(path, target, limit, timeout)
            except (OSError, TimeoutError) as exc:
                logger.info("MP4 faststart skipped (%s)", type(exc).__name__)
            yield target if optimized else path
        finally:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove temporary faststart output")
