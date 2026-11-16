from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import HTTPException, Request
from starlette.requests import ClientDisconnect


async def bounded_body(request: Request, limit: int) -> AsyncIterator[bytes]:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            length = int(declared)
        except ValueError:
            raise HTTPException(400, "Content-Length 无效") from None
        if length < 0:
            raise HTTPException(400, "Content-Length 无效")
        if length > limit:
            raise HTTPException(413, "请求过大")

    stream = request.stream()
    received = 0
    try:
        while True:
            async with asyncio.timeout(60):
                chunk = await anext(stream, None)
            if chunk is None:
                break
            received += len(chunk)
            if received > limit:
                raise HTTPException(413, "请求过大")
            if chunk:
                yield chunk
    except TimeoutError:
        raise HTTPException(408, "读取请求超时") from None
    except ClientDisconnect:
        raise HTTPException(400, "请求连接已断开") from None
