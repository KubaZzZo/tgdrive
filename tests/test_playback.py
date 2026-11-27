from __future__ import annotations

import asyncio

import pytest
from test_downloads import make_file

from app.api.routes.files import FileStreamingResponse
from app.services import playback
from app.services.media_cache import MediaSliceCache


def test_playback_counters_expire_and_are_bounded(monkeypatch):
    now = 100.0
    monkeypatch.setattr(playback, "monotonic", lambda: now)
    transfers = playback.PlaybackTransfers(capacity=2, ttl=5)
    a, b, c = ("a", "video", "session"), ("b", "video", "session"), ("a", "other", "session")
    assert transfers.snapshot(a)["bytes"] is None
    transfers.record(a, 10)
    transfers.record(b, 20)
    transfers.record(a, 5)
    transfers.record(c, 30)
    assert transfers.snapshot(a)["bytes"] == 15
    assert transfers.snapshot(b)["bytes"] is None
    assert transfers.snapshot(c)["bytes"] == 30
    now += 5
    assert transfers.snapshot(a) == {"bytes": None, "sampled_at": 105000}
    transfers.record(a, 1)
    assert transfers.snapshot(a)["bytes"] == 1
    now += 4
    assert transfers.snapshot(a)["bytes"] == 1
    now += 4
    assert transfers.snapshot(a)["bytes"] == 1


def test_playback_counts_ranges_and_isolates_sessions(authenticated_client):
    client, app, storage = authenticated_client
    item = make_file(app, storage)
    session = "a" * 32
    metrics = f"/api/files/{item['id']}/playback/{session}"
    url = f"/api/files/{item['id']}/download?playback_session={session}"
    assert client.get(metrics).json()["bytes"] is None
    assert client.head(url).status_code == 200
    assert client.get(metrics).json()["bytes"] is None
    response = client.get(url, headers={"Range": "bytes=2-7"})
    assert response.content == b"cdefgh"
    assert response.headers["cache-control"] == "private, no-store"
    assert client.get(metrics).json()["bytes"] == 6
    assert client.get(url, headers={"Range": "bytes=8-9"}).content == b"ij"
    response = client.get(metrics)
    assert response.json()["bytes"] == 8
    assert response.headers["cache-control"] == "no-store"
    assert client.get(metrics.replace(session, "b" * 32)).json()["bytes"] is None
    assert client.get(url + "&download=1").status_code == 200
    assert client.get(metrics).json()["bytes"] == 8
    assert client.get(metrics.replace(session, "invalid")).status_code == 422
    assert client.get(metrics.replace(item["id"], "missing")).status_code == 404
    assert client.get(metrics, headers={"X-Workspace-ID": "other"}).status_code == 409
    client.cookies.clear()
    assert client.get(metrics).status_code == 401


def test_playback_counts_cached_delivery_without_counting_readahead(authenticated_client, workdir):
    client, app, storage = authenticated_client
    item = make_file(app, storage)
    app.state.media_cache = MediaSliceCache(workdir / "cache", 8192, 0, slice_bytes=4)
    session = "c" * 32
    metrics = f"/api/files/{item['id']}/playback/{session}"
    url = f"/api/files/{item['id']}/download?playback_session={session}"
    assert client.get(url, headers={"Range": "bytes=0-2"}).content == b"abc"
    assert client.get(metrics).json()["bytes"] == 3
    assert client.get(url, headers={"Range": "bytes=1-3"}).content == b"bcd"
    assert client.get(metrics).json()["bytes"] == 6
    assert app.state.media_cache.snapshot()["hit_bytes"] == 3


def test_failed_send_does_not_count_undelivered_bytes():
    async def run():
        closed = False
        counts = []

        async def stream():
            nonlocal closed
            try:
                yield b"first"
                yield b"second"
            finally:
                closed = True

        async def send(message):
            if message.get("body") == b"second":
                raise OSError("disconnected")

        response = FileStreamingResponse(stream())
        response.record_bytes = counts.append
        with pytest.raises(OSError, match="disconnected"):
            await response.stream_response(send)
        assert counts == [5]
        assert closed

    asyncio.run(run())
