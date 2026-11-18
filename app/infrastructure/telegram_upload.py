from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import BinaryIO

from telethon import TelegramClient, helpers
from telethon.network import MTProtoSender
from telethon.tl.functions.upload import SaveBigFilePartRequest
from telethon.tl.types import InputFileBig

UPLOAD_PART_BYTES = 512 * 1024
PARALLEL_UPLOAD_MIN_BYTES = 32 * 1024 * 1024
logger = logging.getLogger("tgdrive.telegram_upload")


async def upload_parallel(
    client: TelegramClient,
    reader: BinaryIO,
    size: int,
    name: str,
    connections: int,
    report_progress: Callable[[int, int], Awaitable[None]],
) -> InputFileBig:
    """Upload a large segment to the session DC using independent MTProto connections.

    Follows Telethon-FastUpload's multi-sender/512 KiB approach, without its
    global filename or extra dependency. The caller keeps segments below 32 MiB
    on Telethon's standard uploader (including its small-file checksum handling).
    Uses Telethon 1.x private APIs; keep sender lifecycle tests when upgrading.
    """
    if size <= 0 or not 1 <= connections <= 8:
        raise ValueError("Positive size and 1-8 upload connections are required")
    file_id = helpers.generate_random_long()
    part_count = (size + UPLOAD_PART_BYTES - 1) // UPLOAD_PART_BYTES
    parts = iter(range(part_count))
    confirmed = 0
    progress_lock = asyncio.Lock()

    async def worker(sender: MTProtoSender) -> None:
        nonlocal confirmed
        for index in parts:
            # One asyncio loop: next(parts) and synchronous reader.read() have
            # no intervening await, so allocation + read cannot interleave.
            # This is NOT thread-safe. Do not insert an await/offload here.
            # Async reads need a lock around BOTH allocation and reading, or
            # independent file handles positioned at each assigned offset.
            expected = min(UPLOAD_PART_BYTES, size - index * UPLOAD_PART_BYTES)
            block = reader.read(expected)
            if len(block) != expected:
                raise OSError("文件提前结束，无法完成 Telegram 上传")
            request = SaveBigFilePartRequest(file_id, index, part_count, block)
            # Retain Telethon's RPC retry/FloodWait handling on each sender.
            if not await client._call(sender, request):
                raise OSError(f"Telegram 未保存文件分片 {index + 1}/{part_count}")
            async with progress_lock:
                confirmed += len(block)
                # Serialize async callbacks too: an older update must not
                # complete after a newer one and regress visible progress.
                await report_progress(confirmed, size)

    async def disconnect(sender: MTProtoSender) -> None:
        try:
            await sender.disconnect()
        except Exception:
            # A cleanup error must not hide the original upload/cancel error.
            logger.warning("Failed to disconnect Telegram upload sender", exc_info=True)

    session = client.session
    async with AsyncExitStack() as cleanup:
        senders = []
        for _ in range(min(connections, part_count)):
            sender = MTProtoSender(
                session.auth_key, loggers=client._log, connect_timeout=client._timeout
            )
            cleanup.push_async_callback(disconnect, sender)
            await sender.connect(
                client._connection(
                    session.server_address,
                    session.port,
                    session.dc_id,
                    loggers=client._log,
                    proxy=client._proxy,
                    local_addr=client._local_addr,
                )
            )
            senders.append(sender)

        tasks = [asyncio.create_task(worker(sender)) for sender in senders]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    return InputFileBig(file_id, part_count, name)
