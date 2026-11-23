"""Measure MP4 playback and seeks in the built app using synthetic media."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory

import uvicorn
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
DURATION = 150
TARGET = 112
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]


def make_video(ffmpeg: str, path: Path, *, indexed: bool | None, tail_index: bool = False) -> bytes:
    ordinary = indexed is None
    flags = (
        ("0" if tail_index else "+faststart")
        if ordinary
        else "+frag_keyframe+empty_moov+default_base_moof"
    )
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
            f"testsrc2=size=320x180:rate={30 if ordinary else 12}",
            *(["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"] if ordinary else []),
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
            "60" if ordinary else "24",
            "-sc_threshold",
            "0",
            "-movflags",
            flags,
            str(path),
        ],
        check=True,
    )
    data = bytearray(path.read_bytes())
    if ordinary:
        movie = data.index(b"moov") - 4
        assert (movie > len(data) // 2) == tail_index
        assert movie + struct.unpack_from(">I", data, movie)[0] > 65536
        return bytes(data)
    # Empty-movie FFmpeg fixtures leave the duration at zero. Set the movie
    # duration so the player can expose the complete timeline before playback.
    movie = data.index(b"mvhd", 0, 65536) + 4
    assert data[movie] == 0
    scale = struct.unpack_from(">I", data, movie + 12)[0]
    struct.pack_into(">I", data, movie + 16, scale * DURATION)
    return bytes(data)


def exercise(
    base: str, channel: str | None, files: dict, storage, mobile: bool, play_ms: int
) -> list:
    samples = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=channel)
        page = browser.new_page(
            bypass_csp=True, viewport={"width": 390, "height": 844} if mobile else None
        )
        page.on(
            "console",
            lambda message: (
                print(message.text, flush=True) if message.type in ("error", "warning") else None
            ),
        )
        cdp = page.context.new_cdp_session(page)
        cdp.send("Media.enable")
        cdp.on("Media.playerErrorsRaised", lambda event: print("MEDIA", event, flush=True))
        cdp.on(
            "Media.playerMessagesLogged",
            lambda event: (
                print("MEDIA", event, flush=True)
                if any(m["level"] in ("error", "warning") for m in event["messages"])
                else None
            ),
        )
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
        resume_position = None
        for label, name in [
            ("unindexed first seek", "unindexed.mp4"),
            ("unindexed native resume after reload", "unindexed.mp4"),
            ("indexed first seek", "indexed.mp4"),
            ("ordinary MP4 native", "ordinary.mp4"),
            ("ordinary MP4 with tail index", "tail-index.mp4"),
        ]:
            item_id, _ = files[name]
            warm = label == "unindexed native resume after reload"
            if warm:
                page.reload()
            requests = []

            def record(request, item_id=item_id, recorded=requests):
                if f"/files/{item_id}/download" in request.url:
                    recorded.append(request.headers.get("range"))

            page.on("request", record)
            before_cancel = storage.cancelled_bytes
            row = page.get_by_role("button", name=name, exact=True)
            row.wait_for(state="visible")
            started = page.evaluate("performance.now()")
            row.click()
            page.wait_for_function(
                """() => {
                    const video = document.querySelector('.media-screen video');
                    return video?.readyState >= 1 && Math.abs(video.duration - 150) < 1;
                }""",
                timeout=12000,
            )
            video = page.locator(".media-screen video")
            first_frame = (
                video.evaluate(
                    """(video, goal) => new Promise((resolve, reject) => {
                    const timeout = setTimeout(() => reject(new Error('First frame not rendered')), 12000);
                    function frame(now, metadata) {
                        if (goal !== null && Math.abs(metadata.mediaTime - goal) > 1) {
                            video.requestVideoFrameCallback(frame);
                            return;
                        }
                        clearTimeout(timeout);
                        video.pause();
                        resolve(now);
                    }
                    video.requestVideoFrameCallback(frame);
                    video.play().catch(error => { clearTimeout(timeout); reject(error); });
                })""",
                    resume_position if warm else None,
                )
                - started
            )
            if video.evaluate(
                "video => document.pictureInPictureEnabled && !!video.requestPictureInPicture"
            ):
                toolbar = page.get_by_role("group", name="播放器操作")
                expect(toolbar.get_by_role("button", name="画中画", exact=True)).to_be_visible()
            startup_requests = len(requests)
            assert video.evaluate("video => video.src.includes('/download')")
            assert any(value and value.startswith("bytes=") for value in requests), requests
            elapsed = None
            if not warm:
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
            stalled_ms = video.evaluate(
                """async (video, playMs) => {
                    let stalled = 0;
                    let since = null;
                    const wait = () => { since ??= performance.now(); };
                    const play = () => {
                        if (since !== null) stalled += performance.now() - since;
                        since = null;
                    };
                    video.addEventListener('waiting', wait);
                    video.addEventListener('playing', play);
                    await video.play();
                    await new Promise(resolve => setTimeout(resolve, playMs));
                    video.pause();
                    play();
                    video.removeEventListener('waiting', wait);
                    video.removeEventListener('playing', play);
                    return stalled;
                }""",
                play_ms,
            )
            if label == "unindexed first seek":
                resume_position = video.evaluate("(video) => video.currentTime")
                assert TARGET <= resume_position < DURATION - 2
            page.get_by_role("button", name="关闭", exact=True).click()
            video.wait_for(state="detached")
            page.wait_for_timeout(150)
            page.remove_listener("request", record)
            sample = {
                "case": label,
                "first_frame_ms": round(first_frame),
                "seek_to_frame_ms": round(elapsed) if elapsed is not None else None,
                "rebuffer_ms": round(stalled_ms),
                "play_window_ms": play_ms,
                "startup_range_requests": startup_requests,
                "bytes_sent_on_cancelled_reads": storage.cancelled_bytes - before_cancel,
            }
            samples.append(sample)
            print(json.dumps(sample), flush=True)
        verify_slow_native_start(page, cdp)
        browser.close()
    return samples


def verify_slow_native_start(page, cdp) -> None:
    cdp.send(
        "Network.emulateNetworkConditions",
        {"offline": False, "latency": 100, "downloadThroughput": 10000, "uploadThroughput": -1},
    )
    start = time.monotonic()
    page.get_by_role("button", name="tail-index.mp4", exact=True).click()
    try:
        page.wait_for_function(
            """() => {
                const video = document.querySelector('.media-screen video');
                return video?.readyState >= 2 && video.src.includes('/download');
            }""",
            timeout=60000,
        )
        elapsed = time.monotonic() - start
        assert elapsed > 15, "Fixture must take longer than the startup inactivity window"
        print(
            json.dumps(
                {"case": "slow native MP4 with tail index", "first_frame_ms": round(elapsed * 1000)}
            )
        )
    finally:
        cdp.send(
            "Network.emulateNetworkConditions",
            {"offline": False, "latency": 0, "downloadThroughput": -1, "uploadThroughput": -1},
        )
        page.get_by_role("button", name="关闭", exact=True).click()


def main() -> None:
    from conftest import FakeTelegramStorage, make_settings

    from app.main import create_app

    parser = argparse.ArgumentParser()
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--channel", help="Playwright Chromium channel, such as msedge or chrome")
    parser.add_argument("--delay-ms", type=int, default=15, help="Fixture delay per 256 KiB block")
    parser.add_argument("--mobile", action="store_true", help="Use a narrow mobile viewport")
    parser.add_argument("--media-cache-mib", type=int, default=0, help="Enable source slice cache")
    parser.add_argument("--runs", type=int, default=1, help="Number of independent browser samples")
    parser.add_argument(
        "--play-ms", type=int, default=1500, help="Playback window for stall sampling"
    )
    args = parser.parse_args()
    if (
        args.runs < 1
        or not 1 <= args.play_ms <= 30_000
        or args.delay_ms < 0
        or args.media_cache_mib < 0
    ):
        parser.error("runs must be positive, play-ms must be 1–30000, delay-ms must be nonnegative")
    assert (ROOT / "static/dist/index.html").is_file(), "Run npm --prefix frontend run build first"

    class Storage(FakeTelegramStorage):
        cancelled_bytes = 0

        async def iter_download(self, message_id, offset, length, *, prefetch=False):
            sent = 0
            try:
                for start in range(offset, offset + length, 262144):
                    await asyncio.sleep(args.delay_ms / 1000)
                    block = self.files[message_id][start : min(start + 262144, offset + length)]
                    sent += len(block)
                    yield block
            finally:
                if sent < length:
                    self.cancelled_bytes += sent

    with TemporaryDirectory(prefix="tgdrive-video-seek-") as temp, socket.socket() as listener:
        path = Path(temp)
        storage = Storage()
        app = create_app(
            make_settings(
                path,
                frontend_dist=ROOT / "static/dist",
                media_cache_max_mib=args.media_cache_mib,
                media_cache_min_free_mib=0,
            ),
            storage=storage,
        )
        files = {}
        for message_id, (name, indexed) in enumerate(
            [
                ("unindexed.mp4", False),
                ("indexed.mp4", True),
                ("ordinary.mp4", None),
                ("tail-index.mp4", None),
            ],
            start=1,
        ):
            data = make_video(
                args.ffmpeg, path / name, indexed=indexed, tail_index=name == "tail-index.mp4"
            )
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
            files[name] = (item["id"], data)
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
            samples = []
            for _ in range(args.runs):
                samples.extend(
                    exercise(base, args.channel, files, storage, args.mobile, args.play_ms)
                )
            for label in dict.fromkeys(sample["case"] for sample in samples):
                group = [sample for sample in samples if sample["case"] == label]
                metrics = {}
                for key in (
                    "first_frame_ms",
                    "seek_to_frame_ms",
                    "rebuffer_ms",
                    "bytes_sent_on_cancelled_reads",
                ):
                    values = sorted(sample[key] for sample in group if sample[key] is not None)
                    if values:
                        metrics[key] = {
                            "median": median(values),
                            "p95": values[math.ceil(len(values) * 0.95) - 1],
                        }
                print(
                    json.dumps({"summary": label, "samples": len(group), "metrics": metrics}),
                    flush=True,
                )
        finally:
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == "__main__":
    main()
