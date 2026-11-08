"""Measure fragmented MP4 seeks in the built app using local synthetic media."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
DURATION = 150
TARGET = 112
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]


def make_video(ffmpeg: str, path: Path, *, indexed: bool) -> bytes:
    flags = "+frag_keyframe+empty_moov+default_base_moof"
    if indexed:
        flags += "+global_sidx"
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=12",
            "-t",
            str(DURATION),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-threads",
            "1",
            "-b:v",
            "500k",
            "-g",
            "24",
            "-sc_threshold",
            "0",
            "-movflags",
            flags,
            str(path),
        ],
        check=True,
    )
    data = bytearray(path.read_bytes())
    # Empty-movie FFmpeg fixtures leave the duration at zero. Set the movie
    # duration so the player can expose the complete timeline before playback.
    movie = data.index(b"mvhd", 0, 65536) + 4
    assert data[movie] == 0
    scale = struct.unpack_from(">I", data, movie + 12)[0]
    struct.pack_into(">I", data, movie + 16, scale * DURATION)
    return bytes(data)


def exercise(base: str, channel: str | None, files: dict[str, str]) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=channel)
        page = browser.new_page(bypass_csp=True)
        # Avoid background native video requests from the file-card thumbnails.
        page.route(
            "**/thumbnail*",
            lambda route: route.fulfill(
                content_type="image/svg+xml",
                body='<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>',
            ),
        )
        assert page.request.post(
            base + "/api/login", data={"password": "correct-horse-battery-staple"}
        ).ok
        page.goto(base)
        for label, name in [
            ("unindexed first seek", "unindexed.mp4"),
            ("unindexed after reopening", "unindexed.mp4"),
            ("indexed first seek", "indexed.mp4"),
        ]:
            page.get_by_text(name, exact=True).first.dblclick()
            page.wait_for_function(
                """() => {
                    const video = document.querySelector('.media-screen video');
                    return video?.readyState >= 2 && video.duration === 150;
                }"""
            )
            video = page.locator(".media-screen video")
            assert not video.evaluate(
                "(v, t) => Array.from({ length: v.buffered.length }, (_, i) => i)"
                ".some(i => v.buffered.start(i) <= t && t < v.buffered.end(i))",
                TARGET,
            )
            ranges = []

            def record(request, item_id=files[name], recorded=ranges):
                if f"/files/{item_id}/download" in request.url:
                    match = re.fullmatch(r"bytes=(\d+)-(\d+)", request.headers.get("range", ""))
                    if match:
                        start, end = map(int, match.groups())
                        # Ignore in-flight sequential prefetch started before the seek.
                        if start >= 65536 and (start - 65536) % 2**21 != 0:
                            recorded.append((start, end - start + 1))

            page.on("request", record)
            elapsed = video.evaluate(
                """(video, target) => new Promise((resolve, reject) => {
                    const timeline = video.closest('.media-player').querySelector('.media-timeline input');
                    const started = performance.now();
                    const timeout = setTimeout(() => reject(new Error('Target frame was not displayed')), 12000);
                    function frame(_, metadata) {
                        if (Math.abs(metadata.mediaTime - target) > 0.25) {
                            video.requestVideoFrameCallback(frame);
                            return;
                        }
                        clearTimeout(timeout);
                        resolve(performance.now() - started);
                    }
                    video.requestVideoFrameCallback(frame);
                    timeline.value = target;
                    timeline.dispatchEvent(new Event('input', { bubbles: true }));
                    timeline.dispatchEvent(new Event('change', { bubbles: true }));
                })""",
                TARGET,
            )
            page.remove_listener("request", record)
            # The fixture has at least 2 MiB after each target keyframe.
            first_playback = next(i for i, (_, length) in enumerate(ranges) if length == 2**21)
            probes = ranges[:first_playback]
            if label == "unindexed first seek":
                assert probes, ranges
            else:
                assert not probes, ranges
            print(
                json.dumps(
                    {
                        "case": label,
                        "seek_to_frame_ms": round(elapsed),
                        "probe_requests": len(probes),
                        "probe_bytes": sum(length for _, length in probes),
                    }
                ),
                flush=True,
            )
            page.get_by_role("button", name="关闭", exact=True).click()
            video.wait_for(state="detached")
        browser.close()


def main() -> None:
    from conftest import FakeTelegramStorage, make_settings

    from app.main import create_app

    parser = argparse.ArgumentParser()
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--channel", help="Playwright Chromium channel, such as msedge or chrome")
    args = parser.parse_args()
    assert (ROOT / "static/dist/index.html").is_file(), "Run npm --prefix frontend run build first"

    class Storage(FakeTelegramStorage):
        async def iter_download(self, message_id, offset, length):
            for start in range(offset, offset + length, 262144):
                await asyncio.sleep(0.015)
                yield self.files[message_id][start : min(start + 262144, offset + length)]

    with TemporaryDirectory(prefix="tgdrive-video-seek-") as temp, socket.socket() as listener:
        path = Path(temp)
        storage = Storage()
        app = create_app(make_settings(path, frontend_dist=ROOT / "static/dist"), storage=storage)
        files = {}
        for message_id, (name, indexed) in enumerate(
            [("unindexed.mp4", False), ("indexed.mp4", True)], start=1
        ):
            data = make_video(args.ffmpeg, path / name, indexed=indexed)
            storage.files[message_id] = data
            item = app.state.db.create_file(
                name,
                None,
                len(data),
                "video/mp4",
                None,
                str(storage.chat),
                [{"part_index": 0, "tg_message_id": message_id, "size": len(data)}],
            )
            files[name] = item["id"]
        listener.bind(("127.0.0.1", 0))
        base = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="warning"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(0.05)
            assert server.started, "Fixture server failed to start"
            exercise(base, args.channel, files)
        finally:
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == "__main__":
    main()
