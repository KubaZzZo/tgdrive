"""Verify Telegram direct playback with synthetic media and an isolated test server."""

from __future__ import annotations

import argparse
import base64
import json
import re
import socket
import threading
import time
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn
from playwright.sync_api import expect, sync_playwright
from video_seek_browser import ROOT, make_video

MINI_APP = "/drive#tgWebAppVersion=9.0&tgWebAppPlatform=android"
SDK_URL = "https://telegram.org/js/telegram-web-app.js?65"
PASSWORD = "correct-horse-battery-staple"
API_ID = 12345
API_HASH = "a" * 32

MOCK_TELEGRAM = """
const state = window.telegramFixture = { held: 0, aborted: 0, mode: 'play' };
const media = Uint8Array.from(atob(MEDIA_DATA), char => char.charCodeAt(0));
export function createTelegramDirectClient(apiId, apiHash) {
  state.credentials = [apiId, apiHash];
  return {
    importSession: async () => {},
    destroy: async () => {},
  };
}
export async function sendCode(_client, { phone }) {
  state.phone = phone;
  return { type: 'code', phoneCodeHash: 'fixture-code' };
}
export async function revokeTelegramDirectAuthorization(saved) {
  if (saved.session !== 'fixture') throw new Error('Wrong direct session');
  sessionStorage.setItem('telegram_fixture_revoked', '1');
}
export function telegramSessionExpired(error) { return error?.message === 'expired'; }
export function telegramDirectError() { return 'Telegram fixture error'; }
export async function telegramDirectMedia() {
  return {
    size: media.byteLength,
    mime: 'video/mp4',
    async read(offset, end, signal) {
      if (state.mode === 'expired') throw new Error('expired');
      if (state.mode === 'hold') {
        state.held++;
        await new Promise((_, reject) => {
          signal.addEventListener('abort', () => {
            state.aborted++;
            reject(signal.reason);
          }, { once: true });
        });
      }
      return media.slice(offset, Math.min(offset + 262144, end));
    },
  };
}
"""


def open_app(browser, base, mock, *, transport: str, saved_session: bool = True):
    page = browser.new_page(viewport={"width": 390, "height": 844})
    page.set_default_timeout(8000)
    page.context.route(SDK_URL, lambda route: route.fulfill(status=200, body=""))
    page.context.route(
        "**/assets/telegramDirectClient-*.js",
        lambda route: route.fulfill(content_type="application/javascript", body=mock),
    )
    if transport != "worker":
        page.add_init_script(
            "Object.defineProperty(navigator, 'serviceWorker', {value: undefined})"
        )
    if transport == "server":
        page.add_init_script("window.MediaSource = window.ManagedMediaSource = undefined")

    downloads = []
    page.on(
        "request",
        lambda request: downloads.append(request.url) if "/download" in request.url else None,
    )
    assert page.request.post(base + "/api/login", data={"password": PASSWORD}).ok
    page.goto(base + MINI_APP)
    if saved_session:
        page.evaluate(
            """sessionStorage.setItem('tgdrive_telegram_direct_session',
                 JSON.stringify({ apiId: 123, session: 'fixture' }))"""
        )
    page.get_by_role("button", name=re.compile(r"fixture.mp4.*Telegram")).click()
    return page, downloads


def verify_configured_credentials(browser, base, mock):
    page, _ = open_app(browser, base, mock, transport="worker", saved_session=False)
    dialog = page.get_by_role("dialog")
    phone = dialog.get_by_label("Telegram 手机号")
    expect(phone).to_be_visible()
    expect(dialog.get_by_label("API ID")).to_have_count(0)
    expect(dialog.get_by_label("API Hash")).to_have_count(0)
    assert page.evaluate("window.telegramFixture.credentials") is None
    phone.fill("+15555550123")
    with page.expect_response("**/api/telegram-direct/credentials") as result:
        dialog.get_by_role("button", name="获取验证码").click()
    assert result.value.ok and result.value.request.method == "POST"
    expect(dialog.get_by_label("Telegram 验证码")).to_be_visible()
    assert page.evaluate("window.telegramFixture.credentials") == [API_ID, API_HASH]
    assert page.evaluate("window.telegramFixture.phone") == "+15555550123"
    page.close()


def play(page):
    page.get_by_role("button", name="开始播放").click()
    page.wait_for_function("() => document.querySelector('video')?.currentTime > 0.2")
    return page.get_by_role("dialog")


def verify_timer(browser, base, mock):
    page, downloads = open_app(browser, base, mock, transport="worker")
    dialog = play(page)
    path = dialog.locator("video").get_attribute("src")
    assert path and path.startswith("/telegram-media/")
    page.evaluate(
        """(path) => {
          window.telegramFixture.mode = 'hold';
          window.pendingRead = fetch(path, {headers: {Range: 'bytes=2500-2699'}})
            .then(response => response.arrayBuffer()).then(() => 'completed', () => 'cancelled');
        }""",
        path,
    )
    page.wait_for_function("() => window.telegramFixture.held > 0")
    page.clock.install()
    dialog.get_by_role("button", name="播放设置", exact=True).click()
    dialog.get_by_label("定时关闭").select_option("custom")
    dialog.get_by_label("自定义分钟").fill("1")
    dialog.get_by_role("button", name="开始定时", exact=True).click()
    expect(dialog.get_by_role("timer")).to_be_visible()
    page.clock.fast_forward(61_000)
    expect(dialog).to_have_count(0)
    assert page.evaluate("window.pendingRead") == "cancelled"
    assert page.evaluate("window.telegramFixture.aborted") > 0
    assert not downloads
    page.close()


def verify_expiry(browser, base, mock, transport):
    page, downloads = open_app(browser, base, mock, transport=transport)
    dialog = play(page)
    page.evaluate("window.telegramFixture.mode = 'expired'")
    if transport == "worker":
        path = dialog.locator("video").get_attribute("src")
        page.evaluate(
            """(path) => { void fetch(path, {headers: {Range: 'bytes=12-15'}})
              .then(response => response.arrayBuffer()).catch(() => {}); }""",
            path,
        )
    else:
        dialog.locator("video").evaluate("(video) => { video.currentTime = 70; }")
    expect(dialog.get_by_text("Telegram 授权已过期，请重新授权。")).to_be_visible()
    expect(dialog.get_by_label("API ID", exact=True)).to_be_visible()
    assert page.evaluate("sessionStorage.getItem('tgdrive_telegram_direct_session')") is None
    assert not downloads
    page.close()


def verify_fallback_and_logout(browser, base, mock):
    page, downloads = open_app(browser, base, mock, transport="server")
    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_text("当前 WebView 无法直连播放这段视频。")).to_be_visible()
    assert not downloads
    dialog.get_by_role("button", name="经服务器播放", exact=True).click()
    dialog = play(page)
    assert any("/api/files/" in url and "/download" in url for url in downloads)
    dialog.get_by_role("button", name="播放设置", exact=True).click()
    expect(dialog.get_by_label("定时关闭")).to_be_visible()
    dialog.get_by_role("button", name="关闭", exact=True).click()
    tools = page.get_by_role("button", name="工具")
    if tools.is_visible():
        tools.click()
    page.get_by_role("button", name="退出", exact=True).click()
    page.wait_for_function("() => sessionStorage.getItem('telegram_fixture_revoked') === '1'")
    expect(page).to_have_url(re.compile(r"/login(?:\?|$)"))
    assert page.evaluate("sessionStorage.getItem('tgdrive_telegram_direct_session')") is None
    page.close()


def main():
    from conftest import FakeTelegramStorage, make_settings

    from app.api.deps import get_settings
    from app.main import create_app

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--channel", help="Playwright Chromium channel, such as chrome")
    args = parser.parse_args()
    dist = ROOT / "static" / "dist"
    assert (dist / "telegram-media-sw.js").is_file(), "Run npm --prefix frontend run build first"

    class Storage(FakeTelegramStorage):
        async def iter_download(self, message_id, offset, length, *, prefetch=False):
            yield self.files[message_id][offset : offset + length]

        async def thumbnail(self, _message_id):
            return None

    with TemporaryDirectory(prefix="tgdrive-telegram-direct-") as temp, socket.socket() as listener:
        workdir = Path(temp)
        data = make_video(args.ffmpeg, workdir / "fixture.mp4", indexed=None, tail_index=True)
        mock = MOCK_TELEGRAM.replace("MEDIA_DATA", json.dumps(base64.b64encode(data).decode()))
        storage = Storage()
        storage.files[1] = data
        settings = make_settings(
            workdir, frontend_dist=dist, channel_sync_enabled=False, log_level="WARNING"
        )
        app = create_app(settings, storage=storage)
        app.state.db.create_file(
            "fixture.mp4",
            None,
            len(data),
            "video/mp4",
            None,
            str(storage.chat),
            [{"part_index": 0, "tg_message_id": 1, "size": len(data)}],
        )
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
            assert server.started
            with (
                sync_playwright() as playwright,
                playwright.chromium.launch(channel=args.channel) as browser,
            ):
                configured = replace(settings, tg_direct_api_id=API_ID, tg_direct_api_hash=API_HASH)
                app.dependency_overrides[get_settings] = lambda: configured
                verify_configured_credentials(browser, base, mock)
                app.dependency_overrides.pop(get_settings)
                verify_timer(browser, base, mock)
                verify_expiry(browser, base, mock, "worker")
                verify_expiry(browser, base, mock, "mp4")
                verify_fallback_and_logout(browser, base, mock)
        finally:
            server.should_exit = True
            thread.join(timeout=10)
    print("Verified configured credentials, timer, expired sessions, playback fallback and logout.")


if __name__ == "__main__":
    main()
