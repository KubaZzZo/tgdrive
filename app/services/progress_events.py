from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager


class ProgressSignal:
    def __init__(self) -> None:
        self.listeners: set[asyncio.Event] = set()

    def notify(self) -> None:
        for listener in self.listeners:
            listener.set()


@contextmanager
def _listen(signals: tuple[ProgressSignal, ...]) -> Iterator[asyncio.Event]:
    changed = asyncio.Event()
    for signal in signals:
        signal.listeners.add(changed)
    try:
        yield changed
    finally:
        for signal in signals:
            signal.listeners.discard(changed)


async def progress_messages(
    snapshot: Callable[[], dict | None],
    finished: Callable[[dict], bool],
    *signals: ProgressSignal,
) -> AsyncIterator[bytes]:
    previous = None
    deadline = asyncio.get_running_loop().time() + 120
    with _listen(signals) as changed:
        while asyncio.get_running_loop().time() < deadline:
            changed.clear()
            state = snapshot()
            if state is None:
                return
            payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
            if payload != previous:
                yield f"data: {payload}\n\n".encode()
                previous = payload
            if finished(state):
                return
            try:
                await asyncio.wait_for(changed.wait(), timeout=12)
            except TimeoutError:
                yield b": keepalive\n\n"
            # Coalesce download chunks when multiple writes finish together.
            await asyncio.sleep(0.35)
