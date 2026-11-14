from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from io import BytesIO
from time import monotonic

import anyio
from PIL import Image, ImageOps

VIDEO_SAMPLE_BYTES = 8 * 1024 * 1024
THUMBNAIL_SIDE = 320
MAX_IMAGE_PIXELS = 24_000_000
Thumbnail = tuple[bytes, str]


class ThumbnailCache:
    def __init__(self, video_slots: asyncio.Semaphore | None = None) -> None:
        self.slots = asyncio.Semaphore(2)
        self.video_slots = video_slots if video_slots is not None else asyncio.Semaphore(1)
        self.waiting = 0
        self.entries: OrderedDict[tuple, tuple[float, Thumbnail | None]] = OrderedDict()

    def lookup(self, key: tuple) -> tuple[float, Thumbnail | None] | None:
        cached = self.entries.pop(key, None)
        if cached and cached[0] > monotonic():
            self.entries[key] = cached
            return cached
        return None

    async def get(
        self, key: tuple, generate: Callable[[], Awaitable[Thumbnail | None]]
    ) -> Thumbnail | None:
        cached = self.lookup(key)
        if cached:
            return cached[1]
        if self.waiting >= 8:
            return None
        self.waiting += 1
        try:
            async with asyncio.timeout(12), self.slots:
                return await self._generate(key, generate)
        finally:
            self.waiting -= 1

    async def _generate(
        self, key: tuple, generate: Callable[[], Awaitable[Thumbnail | None]]
    ) -> Thumbnail | None:
        cached = self.lookup(key)
        if cached:
            return cached[1]
        worker = asyncio.create_task(generate(), name="thumbnail-worker")
        try:
            result = await worker
        finally:
            if not worker.done():
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
        if result is None or len(result[0]) <= 256 * 1024:
            self.entries[key] = (monotonic() + (3600 if result else 60), result)
            if len(self.entries) > 128:
                self.entries.popitem(last=False)
        return result


def image_thumbnail(data: bytes) -> Thumbnail | None:
    try:
        with Image.open(BytesIO(data), formats=("JPEG", "PNG", "GIF", "WEBP")) as image:
            image.draft("RGB", (THUMBNAIL_SIDE, THUMBNAIL_SIDE))
            if image.width * image.height > MAX_IMAGE_PIXELS:
                return None
            image.thumbnail((THUMBNAIL_SIDE, THUMBNAIL_SIDE), Image.Resampling.LANCZOS)
            still = ImageOps.exif_transpose(image).convert("RGBA")
            output = BytesIO()
            still.save(output, "WEBP", quality=75)
            return output.getvalue(), "image/webp"
    except (Image.DecompressionBombError, OSError, ValueError):
        return None


async def video_thumbnail(data: bytes) -> Thumbnail | None:
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            "cache,pipe",
            "-max_alloc",
            str(64 * 1024 * 1024),
            "-threads",
            "1",
            "-i",
            "cache:pipe:0",
            "-map",
            "0:V:0",
            "-frames:v",
            "1",
            "-filter_threads",
            "1",
            "-vf",
            f"scale={THUMBNAIL_SIDE}:{THUMBNAIL_SIDE}:force_original_aspect_ratio=decrease",
            "-c:v",
            "mjpeg",
            "-f",
            "image2pipe",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return None
    read = asyncio.create_task(process.communicate(data))
    try:
        output, _ = await asyncio.shield(read)
        if output.startswith(b"\xff\xd8\xff") and output.endswith(b"\xff\xd9"):
            return output, "image/jpeg"
        return None
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        with anyio.CancelScope(shield=True):
            await asyncio.shield(read)
