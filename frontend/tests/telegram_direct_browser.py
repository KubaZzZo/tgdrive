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
PHONE = "+8613800138000"
SESSION_KEY = "tgdrive_telegram_direct_session"

MOCK_TELEGRAM = """
const state = window.telegramFixture = {
  held: 0, aborted: 0, mode: 'play', passwordChecks: 0, revoked: [], codeRequests: 0,
  qrCanceled: 0, destroyed: 0,
};
const media = Uint8Array.from(atob(MEDIA_DATA), char => char.charCodeAt(0));
export function createTelegramDirectClient(apiId, apiHash, { onQrAuthorized } = {}) {
  state.credentials = [apiId, apiHash];
  return {
    onQrAuthorized,
    importSession: async (session) => { state.imported = session; },
    exportSession: async () => window.fixtureSession ?? 'fixture',
    destroy: async () => { state.destroyed++; },
  };
}
export async function sendCode(_client, { phone }) {
  state.phone = phone;
  state.codeRequests++;
  return { type: window.fixtureCodeDelivery ?? 'app', phoneCodeHash: 'fixture-code' };
}
export async function signIn(_client, { phoneCode, phoneCodeHash }) {
  if (phoneCode !== '12345' || phoneCodeHash !== 'fixture-code') throw new Error('Wrong code');
  throw new Error('2fa');
}
export function telegramPasswordRequired(error) { return error?.message === '2fa'; }
export async function checkPassword(_client, { password }) {
  if (password !== 'fixture-password') throw new Error('Wrong password');
  state.passwordChecks++;
}
export function signInQr(client, { abortSignal, onUrlUpdated, onQrScanned }) {
  if (!client.onQrAuthorized) throw new Error('Login token updates are disabled');
  let sequence = 0;
  state.rotateQr = () => onUrlUpdated(`tg://login?token=Zml4dHVyZS0${++sequence}`, new Date(Date.now() + 30_000));
  state.rotateQr();
  return new Promise((resolve, reject) => {
    const cancel = () => {
      state.qrCanceled++;
      reject(abortSignal.reason);
    };
    abortSignal.addEventListener('abort', cancel, { once: true });
    state.confirmQr = (needsPassword = false, holdProfile = false) => {
      const finish = (complete) => {
        abortSignal.removeEventListener('abort', cancel);
        complete();
      };
      onQrScanned();
      if (needsPassword) return finish(() => reject(new Error('2fa')));
      client.onQrAuthorized();
      state.finishQr = () => finish(resolve);
      if (!holdProfile) state.finishQr();
    };
  });
}
export async function logOutTelegramDirectClient(client) {
  state.revoked.push(await client.exportSession());
}
export async function revokeTelegramDirectAuthorization(saved) {
  state.revoked.push(saved.session);
  sessionStorage.setItem('telegram_fixture_revoked', '1');
}
export function telegramSessionExpired(error) { return error?.message === 'expired'; }
export function telegramDirectError() { return 'Telegram fixture error'; }
export async function telegramDirectMedia(_client, _item, _signal, onReceived) {
  if (window.fixtureHoldMedia) {
    state.opening = true;
    await new Promise((_, reject) => {
      _signal.addEventListener('abort', () => reject(_signal.reason), {once: true});
    });
  }
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
      const block = media.slice(offset, Math.min(offset + 262144, end));
      onReceived?.(block.length);
      return block;
    },
  };
}
"""


def mock_telegram(context, mock):
    context.route(SDK_URL, lambda route: route.fulfill(status=200, body=""))
    context.route(
        "**/assets/telegramDirectClient-*.js",
        lambda route: route.fulfill(content_type="application/javascript", body=mock),
    )


def open_direct(page):
    page.get_by_label("fixture.mp4的更多操作", exact=True).click()
    page.get_by_role("button", name="Telegram 直连播放", exact=True).click()


def open_app(
    browser,
    base,
    mock,
    *,
    transport: str,
    saved_session: bool = True,
    direct: bool | None = True,
    mini_app: bool = True,
    storage_state: dict | None = None,
):
    viewport = {"width": 390, "height": 844} if mini_app else {"width": 1280, "height": 800}
    page = browser.new_page(viewport=viewport, storage_state=storage_state)
    page.set_default_timeout(8000)
    mock_telegram(page.context, mock)
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
    if storage_state is None:
        assert page.request.post(base + "/api/login", data={"password": PASSWORD}).ok
    page.goto(base + (MINI_APP if mini_app else "/drive"))
    if saved_session:
        page.evaluate(
            """sessionStorage.setItem('tgdrive_telegram_direct_session',
                 JSON.stringify({ apiId: 123, session: 'fixture' }))"""
        )
    if direct:
        open_direct(page)
    elif direct is False:
        page.get_by_role("button", name="fixture.mp4", exact=True).click()
    return page, downloads


def open_authorization(page):
    tools = page.get_by_role("button", name="工具", exact=True)
    account = page.get_by_role("button", name="Telegram 账号", exact=True)
    expect(tools.or_(account).filter(visible=True).first).to_be_visible()
    if not account.is_visible():
        tools.click()
    account.click()
    page.get_by_role("button", name="Telegram 直连授权", exact=True).click()
    return page.get_by_role("dialog", name="Telegram 直连授权")


def request_code(page, dialog):
    phone = dialog.get_by_label("Telegram 手机号")
    phone.fill(PHONE)
    with page.expect_response("**/api/telegram-direct/credentials") as result:
        dialog.get_by_role("button", name="获取验证码").click()
    assert result.value.ok and result.value.request.method == "POST"
    expect(dialog.get_by_label("Telegram 验证码")).to_be_visible()


def enter_code(dialog):
    dialog.get_by_label("Telegram 验证码").fill("12345")
    dialog.get_by_role("button", name="授权此设备", exact=True).click()
    dialog.get_by_label("Telegram 两步验证密码").fill("fixture-password")


def verify_configured_credentials(browser, base, mock):
    page, _ = open_app(browser, base, mock, transport="worker", saved_session=False, direct=None)
    dialog = open_authorization(page)
    expect(dialog.get_by_label("Telegram 手机号")).to_be_visible()
    expect(dialog.get_by_label("API ID")).to_have_count(0)
    expect(dialog.get_by_label("API Hash")).to_have_count(0)
    assert page.evaluate("window.telegramFixture === undefined")
    request_code(page, dialog)
    expect(dialog.get_by_text("打开官方「Telegram」对话查收验证码", exact=False)).to_be_visible()
    enter_code(dialog)
    assert page.evaluate("window.telegramFixture.credentials") == [API_ID, API_HASH]
    assert page.evaluate("window.telegramFixture.phone") == PHONE
    dialog.get_by_role("button", name="授权此设备", exact=True).click()
    expect(dialog.get_by_text("当前设备已授权 Telegram 直连播放。")).to_be_visible()
    storage_state = page.context.storage_state()
    page.close()

    storage_state["cookies"] = []
    page, downloads = open_app(
        browser,
        base,
        mock,
        transport="worker",
        saved_session=False,
        direct=None,
        storage_state=storage_state,
    )
    expect(page).to_have_url(re.compile(r"/login(?:\?|$)"))
    assert page.evaluate("key => JSON.parse(localStorage.getItem(key))?.session", SESSION_KEY) == (
        "fixture"
    )
    assert page.request.post(base + "/api/login", data={"password": PASSWORD}).ok
    page.goto(base + MINI_APP)
    open_direct(page)
    dialog = play(page)
    assert page.evaluate("window.telegramFixture.imported") == "fixture"
    assert page.evaluate("sessionStorage.getItem('tgdrive_telegram_direct_session')") is None
    assert not downloads
    dialog.get_by_role("button", name="关闭", exact=True).click()
    page.get_by_role("button", name="工具", exact=True).click()
    page.get_by_role("button", name="退出", exact=True).click()
    page.wait_for_function("() => sessionStorage.getItem('telegram_fixture_revoked') === '1'")
    expect(page).to_have_url(re.compile(r"/login(?:\?|$)"))
    assert page.evaluate("key => JSON.parse(localStorage.getItem(key))", SESSION_KEY) is None
    page.close()


def start_qr(dialog):
    dialog.get_by_role("button", name="Telegram 扫码授权", exact=True).click()
    image = dialog.get_by_role("img", name="Telegram 登录二维码", exact=True)
    expect(image).to_be_visible()
    assert image.evaluate("async img => { await img.decode(); return img.naturalWidth; }") > 0
    return image


def verify_qr_authorization(browser, base, mock):
    page, _ = open_app(browser, base, mock, transport="worker", saved_session=False, direct=None)
    dialog = open_authorization(page)
    image = start_qr(dialog)
    assert page.evaluate("window.telegramFixture.credentials") == [API_ID, API_HASH]
    assert page.evaluate("window.telegramFixture.codeRequests") == 0
    original = image.get_attribute("src")
    page.evaluate("window.telegramFixture.rotateQr()")
    expect(image).not_to_have_attribute("src", original)
    dialog.get_by_role("button", name="返回授权方式").click()
    page.wait_for_function(
        "window.telegramFixture.qrCanceled === 1 && window.telegramFixture.destroyed === 1"
    )
    assert page.evaluate("key => localStorage.getItem(key)", SESSION_KEY) is None

    page.evaluate("window.fixtureCodeDelivery = 'sms'")
    request_code(page, dialog)
    expect(dialog.get_by_text("Telegram 已通过短信发送验证码", exact=False)).to_be_visible()
    start_qr(dialog)
    assert page.evaluate("window.telegramFixture.codeRequests") == 1
    page.evaluate("window.telegramFixture.confirmQr(true)")
    dialog.get_by_label("Telegram 两步验证密码").fill("fixture-password")
    dialog.get_by_role("button", name="授权此设备", exact=True).click()
    expect(dialog.get_by_text("当前设备已授权 Telegram 直连播放。")).to_be_visible()
    assert page.evaluate("window.telegramFixture.passwordChecks") == 1
    page.reload()
    expect(
        open_authorization(page).get_by_text("当前设备已授权 Telegram 直连播放。")
    ).to_be_visible()
    page.close()


def verify_shared_qr_authorization(browser, base, mock):
    with browser.new_context() as context:
        mock_telegram(context, mock)
        assert context.request.post(base + "/api/login", data={"password": PASSWORD}).ok
        first, second = context.new_page(), context.new_page()
        first.goto(base + "/drive")
        second.goto(base + "/drive")
        second.evaluate("window.fixtureSession = 'fixture-second'")
        first_dialog = open_authorization(first)
        second_dialog = open_authorization(second)
        start_qr(first_dialog)
        start_qr(second_dialog)
        for page, dialog in ((first, first_dialog), (second, second_dialog)):
            page.evaluate("window.telegramFixture.confirmQr()")
            expect(dialog.get_by_text("当前设备已授权 Telegram 直连播放。")).to_be_visible()
        second.wait_for_function("window.telegramFixture.revoked.includes('fixture-second')")
        assert (
            second.evaluate("key => JSON.parse(localStorage.getItem(key)).session", SESSION_KEY)
            == "fixture"
        )


def verify_confirmed_qr_cancellation(browser, base, mock, *, mini_app):
    page, _ = open_app(
        browser, base, mock, transport="worker", saved_session=False, direct=None, mini_app=mini_app
    )
    dialog = open_authorization(page)
    start_qr(dialog)
    page.evaluate("window.telegramFixture.confirmQr(false, true)")
    expect(dialog.get_by_role("status")).to_have_text("正在确认 Telegram 授权…")
    dialog.get_by_role("button", name="返回授权方式").click()
    page.wait_for_function("window.telegramFixture.destroyed === 1")
    assert page.evaluate("window.telegramFixture.revoked") == ["fixture"]
    assert page.evaluate("key => localStorage.getItem(key)", SESSION_KEY) is None
    page.close()


def verify_qr_invalidated_by_revocation(browser, base, mock, *, logout, needs_password=False):
    with browser.new_context(viewport={"width": 390, "height": 844}) as context:
        mock_telegram(context, mock)
        assert context.request.post(base + "/api/login", data={"password": PASSWORD}).ok
        first, second = context.new_page(), context.new_page()
        first.goto(base + MINI_APP)
        second.goto(base + MINI_APP)
        first_dialog, second_dialog = open_authorization(first), open_authorization(second)
        start_qr(first_dialog)
        first.evaluate(
            "needsPassword => window.telegramFixture.confirmQr(needsPassword, true)", needs_password
        )
        if needs_password:
            expect(first_dialog.get_by_label("Telegram 两步验证密码")).to_be_visible()
        else:
            expect(first_dialog.get_by_role("status")).to_have_text("正在确认 Telegram 授权…")

        start_qr(second_dialog)
        second.evaluate("window.telegramFixture.confirmQr()")
        expect(second_dialog.get_by_text("当前设备已授权 Telegram 直连播放。")).to_be_visible()
        if logout:
            for dialog in (
                second_dialog,
                second.get_by_role("dialog", name="Telegram 账号与存储频道"),
            ):
                dialog.get_by_role("button", name="关闭", exact=True).click()
            second.get_by_role("button", name="工具", exact=True).click()
            second.get_by_role("button", name="退出", exact=True).click()
            expect(second).to_have_url(re.compile(r"/login(?:\?|$)"))
            assert context.request.post(base + "/api/login", data={"password": PASSWORD}).ok
        else:
            second_dialog.get_by_role("button", name="撤销直连授权").click()
            expect(second_dialog.get_by_label("Telegram 手机号")).to_be_visible()
        assert second.evaluate("window.telegramFixture.revoked") == ["fixture"]

        if needs_password:
            first_dialog.get_by_label("Telegram 两步验证密码").fill("fixture-password")
            first_dialog.get_by_role("button", name="授权此设备", exact=True).click()
        else:
            first.evaluate("window.telegramFixture.finishQr()")
        expect(first_dialog.get_by_role("alert")).to_have_text("设备直连已撤销，请重新授权。")
        expect(first_dialog.get_by_label("Telegram 手机号")).to_be_visible()
        first.wait_for_function("window.telegramFixture.destroyed === 1")
        assert first.evaluate("window.telegramFixture.passwordChecks") == 0
        assert first.evaluate("window.telegramFixture.revoked") == (
            [] if needs_password else ["fixture"]
        )
        assert first.evaluate("key => JSON.parse(localStorage.getItem(key))", SESSION_KEY) is None


def verify_shared_authorization(browser, base, mock):
    with browser.new_context(viewport={"width": 390, "height": 844}) as context:
        mock_telegram(context, mock)
        assert context.request.post(base + "/api/login", data={"password": PASSWORD}).ok
        first = context.new_page()
        second = context.new_page()
        first.goto(base + MINI_APP)
        second.goto(base + MINI_APP)
        second.evaluate("window.fixtureSession = 'fixture-second'")
        first_dialog = open_authorization(first)
        second_dialog = open_authorization(second)
        for page, dialog in ((first, first_dialog), (second, second_dialog)):
            request_code(page, dialog)
            enter_code(dialog)

        for dialog in (first_dialog, second_dialog):
            dialog.get_by_role("button", name="授权此设备", exact=True).click()
            expect(dialog.get_by_text("当前设备已授权 Telegram 直连播放。")).to_be_visible()
        assert first.evaluate("window.telegramFixture.passwordChecks") == 1
        assert second.evaluate("window.telegramFixture.passwordChecks") == 0
        assert (
            second.evaluate("key => JSON.parse(localStorage.getItem(key))?.session", SESSION_KEY)
            == "fixture"
        )

        for page, session in ((first, "older-tab"), (second, "legacy")):
            page.evaluate(
                "([key, session]) => sessionStorage.setItem(key, JSON.stringify({ apiId: 123, session }))",
                [SESSION_KEY, session],
            )
        second_dialog.get_by_role("button", name="撤销直连授权", exact=True).click()
        expect(second_dialog.get_by_label("Telegram 手机号")).to_be_visible()
        assert second.evaluate("window.telegramFixture.revoked") == ["legacy", "fixture"]
        assert second.evaluate("key => sessionStorage.getItem(key)", SESSION_KEY) is None

        first_dialog.get_by_role("button", name="关闭", exact=True).click()
        first.get_by_role("dialog").get_by_role("button", name="关闭", exact=True).click()
        open_direct(first)
        expect(
            first.get_by_text("当前设备尚未授权 Telegram 直连，已改用服务器播放。")
        ).to_be_visible()
        assert first.evaluate("window.telegramFixture.imported === undefined")


def play(page):
    page.get_by_role("button", name="开始播放").click()
    page.wait_for_function("() => document.querySelector('video')?.currentTime > 0.2")
    return page.get_by_role("dialog")


def start_close_timer(page, dialog):
    page.clock.install()
    dialog.get_by_role("button", name="播放设置", exact=True).click()
    dialog.get_by_label("定时关闭").select_option("custom")
    dialog.get_by_label("自定义分钟").fill("1")
    dialog.get_by_role("button", name="开始定时", exact=True).click()
    expect(dialog.get_by_role("timer")).to_be_visible()


def close_by_timer(page, dialog):
    start_close_timer(page, dialog)
    page.clock.fast_forward(61_000)
    expect(dialog).to_have_count(0)


def verify_default_playback(browser, base, mock):
    page, downloads = open_app(
        browser, base, mock, transport="worker", saved_session=False, direct=False
    )
    dialog = play(page)
    assert any("/api/files/" in url and "/download" in url for url in downloads)
    close_by_timer(page, dialog)

    page.get_by_role("button", name="网格视图").click()
    page.locator(".file-card-open").get_by_text("fixture.mp4", exact=True).click()
    expect(dialog.get_by_role("button", name="开始播放")).to_be_visible()
    assert page.evaluate("window.telegramFixture === undefined")
    assert page.evaluate("sessionStorage.getItem('tgdrive_telegram_direct_session')") is None
    page.close()


def verify_unauthorized_playback(browser, base, mock, *, mini_app: bool):
    page, downloads = open_app(
        browser, base, mock, transport="worker", saved_session=False, mini_app=mini_app
    )
    expect(page.get_by_text("当前设备尚未授权 Telegram 直连，已改用服务器播放。")).to_be_visible()
    dialog = play(page)
    expect(dialog.get_by_label("Telegram 手机号")).to_have_count(0)
    assert any("/api/files/" in url and "/download" in url for url in downloads)
    assert page.evaluate("window.telegramFixture === undefined")
    close_by_timer(page, dialog)
    page.close()


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
    close_by_timer(page, dialog)
    assert page.evaluate("window.pendingRead") == "cancelled"
    assert page.evaluate("window.telegramFixture.aborted") > 0
    assert not downloads
    page.close()


def verify_expiry(browser, base, mock, transport):
    page, downloads = open_app(browser, base, mock, transport=transport)
    dialog = play(page)
    start_close_timer(page, dialog)
    page.clock.fast_forward(20_000)
    assert not downloads
    dialog.locator("video").evaluate("video => video.pause()")
    if transport == "worker":
        dialog.locator("video").evaluate("video => { video.currentTime = 37; }")
        page.wait_for_function("() => !document.querySelector('video').seeking")
        path = dialog.locator("video").get_attribute("src")
        page.evaluate(
            """(path) => { window.telegramFixture.mode = 'expired';
              void fetch(path, {headers: {Range: 'bytes=12-15'}})
              .then(response => response.arrayBuffer()).catch(() => {}); }""",
            path,
        )
    else:
        dialog.locator("video").evaluate(
            "(video) => { window.telegramFixture.mode = 'expired'; video.currentTime = 70; }"
        )
    expect(page.get_by_text("Telegram 直连授权已失效，已改用服务器播放。")).to_be_visible()
    expect(dialog.get_by_label("API ID", exact=True)).to_have_count(0)
    assert page.evaluate("sessionStorage.getItem('tgdrive_telegram_direct_session')") is None
    expected_time = 37 if transport == "worker" else 70
    try:
        page.wait_for_function(
            "time => { const v = document.querySelector('video'); return v?.readyState >= 2 && Math.abs(v.currentTime - time) < 1 && v.paused; }",
            arg=expected_time,
        )
    except Exception:
        print("Expiry state", transport, video_state(page))
        raise
    assert page.evaluate(
        "() => Object.values(JSON.parse(localStorage.getItem('tgdrive_playback_preferences_v1')).videos).includes('telegram')"
    )
    assert any("/api/files/" in url and "/download" in url for url in downloads)
    expect(dialog.get_by_role("timer")).to_be_visible()
    page.clock.fast_forward(41_000)
    expect(dialog).to_have_count(0)
    page.close()


def video_state(page):
    return page.locator("video").evaluate(
        "v => ({time: v.currentTime, ready: v.readyState, paused: v.paused, src: v.src, error: v.error?.message})"
    )


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


def verify_playback_switching(browser, base, mock, transport, screenshots):
    page, _ = open_app(
        browser, base, mock, transport=transport, direct=False, mini_app=transport == "worker"
    )
    dialog = page.get_by_role("dialog")
    video = dialog.locator("video")
    page.wait_for_function("() => document.querySelector('video')?.readyState >= 2")
    expect(dialog.get_by_label("播放传输状态")).to_contain_text(re.compile(r"服务器发送 .*/s"))
    expect(dialog.get_by_label("播放传输状态")).to_contain_text(re.compile(r"已缓冲 \d+ 秒"))
    video.evaluate("video => { video.currentTime = 36; video.volume = 0.35; video.muted = true; }")
    page.wait_for_function("() => !document.querySelector('video').seeking")
    dialog.get_by_role("button", name="播放设置", exact=True).click()
    dialog.get_by_label("播放速度", exact=True).select_option("1.5")
    dialog.get_by_label("播放方式", exact=True).select_option("telegram")
    page.wait_for_function(
        "() => { const v = document.querySelector('video'); return v?.readyState >= 2 && Math.abs(v.currentTime - 36) < 0.5 && v.paused && v.playbackRate === 1.5 && v.muted && Math.abs(v.volume - 0.35) < 0.01; }"
    )
    expect(dialog.get_by_label("播放传输状态")).to_contain_text(re.compile(r"Telegram 下载 .*/s"))
    source = video.get_attribute("src")
    assert source and source.startswith("/telegram-media/" if transport == "worker" else "blob:")
    dialog.get_by_role("button", name="播放", exact=True).click()
    page.wait_for_function("() => document.querySelector('video').currentTime > 36.3")
    dialog.get_by_role("button", name="播放设置", exact=True).click()
    position = video.evaluate("video => video.currentTime")
    dialog.get_by_label("播放方式", exact=True).select_option("server")
    page.wait_for_function(
        "time => { const v = document.querySelector('video'); return v?.src.includes('/download') && v.readyState >= 2 && !v.paused && v.currentTime >= time && v.currentTime < time + 8 && v.playbackRate === 1.5; }",
        arg=position,
    )
    dialog.get_by_role("button", name="暂停", exact=True).click()
    dialog.get_by_role("button", name="播放设置", exact=True).click()
    dialog.get_by_role("button", name="从头播放", exact=True).click()
    dialog.get_by_label("播放方式", exact=True).select_option("telegram")
    page.wait_for_function(
        "() => { const v = document.querySelector('video'); return v?.readyState >= 2 && v.currentTime < 0.1 && v.paused; }"
    )
    # Reopening uses the explicit video preference, even with a different global default.
    dialog.get_by_role("button", name="关闭", exact=True).click()
    page.get_by_role("button", name="fixture.mp4", exact=True).click()
    expect(dialog.get_by_label("播放传输状态")).to_contain_text("Telegram 下载")
    dialog.get_by_role("button", name="播放设置", exact=True).click()
    expect(dialog.get_by_label("默认播放方式", exact=True)).to_have_value("server")
    expect(dialog.get_by_label("播放方式", exact=True)).to_have_value("telegram")
    if screenshots:
        screenshots.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(screenshots / f"playback-{transport}-settings.png"))
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    dialog.get_by_role("button", name="此视频跟随默认", exact=True).click()
    dialog.get_by_role("button", name="关闭", exact=True).click()
    page.get_by_role("button", name="fixture.mp4", exact=True).click()
    expect(dialog.get_by_label("播放传输状态")).to_contain_text("服务器发送")
    dialog.get_by_role("button", name="播放设置", exact=True).click()
    dialog.get_by_label("默认播放方式", exact=True).select_option("telegram")
    dialog.get_by_role("button", name="关闭", exact=True).click()
    page.get_by_role("button", name="fixture.mp4", exact=True).click()
    expect(dialog.get_by_label("播放传输状态")).to_contain_text("Telegram 下载")
    dialog.get_by_role("button", name="关闭", exact=True).click()
    page.evaluate("sessionStorage.removeItem('tgdrive_telegram_direct_session')")
    page.get_by_role("button", name="fixture.mp4", exact=True).click()
    expect(dialog.get_by_label("播放传输状态")).to_contain_text("服务器发送")
    dialog.get_by_role("button", name="播放设置", exact=True).click()
    expect(dialog.get_by_label("默认播放方式", exact=True)).to_have_value("telegram")
    expect(dialog.get_by_label("播放方式", exact=True).locator('option[value="telegram"]')).to_have_attribute("disabled", "")
    if screenshots:
        page.screenshot(path=str(screenshots / f"playback-{transport}-fallback.png"))
    page.close()


def verify_timer_during_switch(browser, base, mock):
    page, _ = open_app(browser, base, mock, transport="worker", direct=False)
    dialog = play(page)
    start_close_timer(page, dialog)
    page.clock.fast_forward(50_000)
    page.evaluate("window.fixtureHoldMedia = true")
    dialog.get_by_label("播放方式", exact=True).select_option("telegram")
    page.wait_for_function("() => window.telegramFixture?.opening")
    expect(dialog.get_by_text("正在准备 Telegram 直连播放…", exact=True)).to_be_visible()
    page.clock.fast_forward(11_000)
    expect(dialog).to_have_count(0)
    page.close()


def verify_switch_before_metadata(browser, base, mock):
    page, _ = open_app(browser, base, mock, transport="worker", direct=False)
    dialog = page.get_by_role("dialog")
    page.wait_for_function("() => document.querySelector('video')?.readyState >= 2")
    dialog.locator("video").evaluate("video => { video.currentTime = 44; }")
    page.wait_for_function("() => !document.querySelector('video').seeking")
    dialog.get_by_role("button", name="关闭", exact=True).click()
    held = []
    page.route("**/api/files/*/download?*", lambda route: held.append(route))
    page.get_by_role("button", name="fixture.mp4", exact=True).click()
    expect(dialog.get_by_role("button", name="播放设置", exact=True)).to_be_visible()
    assert video_state(page)["ready"] == 0
    dialog.get_by_role("button", name="播放设置", exact=True).click()
    dialog.get_by_label("播放方式", exact=True).select_option("telegram")
    page.wait_for_function(
        "() => { const v = document.querySelector('video'); return v?.readyState >= 2 && Math.abs(v.currentTime - 44) < 0.5 && v.paused; }"
    )
    for route in held:
        route.abort()
    page.close()


def main():
    from conftest import FakeTelegramStorage, make_settings

    from app.api.deps import get_settings
    from app.main import create_app

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--channel", help="Playwright Chromium channel, such as chrome")
    parser.add_argument("--screenshots-dir", type=Path)
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
        server = uvicorn.Server(
            uvicorn.Config(app, access_log=False, log_level="warning", timeout_graceful_shutdown=5)
        )
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
                verify_unauthorized_playback(browser, base, mock, mini_app=True)
                verify_unauthorized_playback(browser, base, mock, mini_app=False)
                verify_default_playback(browser, base, mock)
                verify_playback_switching(browser, base, mock, "worker", args.screenshots_dir)
                verify_playback_switching(browser, base, mock, "mp4", args.screenshots_dir)
                verify_switch_before_metadata(browser, base, mock)
                verify_configured_credentials(browser, base, mock)
                verify_qr_authorization(browser, base, mock)
                verify_shared_qr_authorization(browser, base, mock)
                verify_confirmed_qr_cancellation(browser, base, mock, mini_app=False)
                verify_confirmed_qr_cancellation(browser, base, mock, mini_app=True)
                verify_qr_invalidated_by_revocation(browser, base, mock, logout=False)
                verify_qr_invalidated_by_revocation(browser, base, mock, logout=True)
                verify_qr_invalidated_by_revocation(
                    browser, base, mock, logout=False, needs_password=True
                )
                verify_shared_authorization(browser, base, mock)
                app.dependency_overrides.pop(get_settings)
                verify_timer(browser, base, mock)
                verify_timer_during_switch(browser, base, mock)
                verify_expiry(browser, base, mock, "worker")
                verify_expiry(browser, base, mock, "mp4")
                verify_fallback_and_logout(browser, base, mock)
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive(), "Fixture server did not shut down"
    print(
        "Verified playback stats, switching, preferences, authorization, expiry, timers and logout."
    )


if __name__ == "__main__":
    main()
