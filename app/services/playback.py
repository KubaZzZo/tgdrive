from __future__ import annotations

from collections import OrderedDict
from time import monotonic


class PlaybackTransfers:
    """Short-lived, bounded counters for bytes sent by individual playback sessions."""

    def __init__(self, *, capacity: int = 256, ttl: float = 120):
        self.capacity = capacity
        self.ttl = ttl
        self._sessions: OrderedDict[tuple[str, str, str], tuple[int, float]] = OrderedDict()

    def _expire(self, now: float) -> None:
        while self._sessions:
            _, touched = next(iter(self._sessions.values()))
            if now - touched < self.ttl:
                break
            self._sessions.popitem(last=False)

    def record(self, key: tuple[str, str, str], size: int) -> None:
        now = monotonic()
        self._expire(now)
        total, _ = self._sessions.pop(key, (0, now))
        self._sessions[key] = (total + size, now)
        while len(self._sessions) > self.capacity:
            self._sessions.popitem(last=False)

    def snapshot(self, key: tuple[str, str, str]) -> dict:
        now = monotonic()
        self._expire(now)
        counter = self._sessions.pop(key, None)
        if counter is not None:
            self._sessions[key] = (counter[0], now)
        return {"bytes": counter[0] if counter else None, "sampled_at": now * 1000}
