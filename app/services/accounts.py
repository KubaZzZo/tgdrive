from __future__ import annotations

import asyncio
import base64
import io
import logging
import re
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs

import qrcode
from fastapi import HTTPException
from starlette.responses import JSONResponse
from telethon import TelegramClient, events, functions, types, utils
from telethon.errors import FloodWaitError, PasswordHashInvalidError, SessionPasswordNeededError
from telethon.tl.types import Channel

from app.config import Settings
from app.db import Database
from app.network import get_proxy
from app.services.channel_sync import ChannelSyncService
from app.services.uploads import UploadService
from app.storage import TelegramStorage

logger = logging.getLogger("tgdrive.accounts")


@dataclass(eq=False)
class Runtime:
    db: Database
    storage: Any
    uploads: UploadService
    sync: ChannelSyncService
    leases: int = 0
    retired: bool = False


def account_settings(settings: Settings, profile: dict | None) -> Settings:
    if not profile:
        return settings
    if not profile["session_key"]:
        return replace(settings, tg_chat_id=profile["chat_id"])
    key = profile["session_key"]
    if not re.fullmatch(r"[a-f0-9]{32}", key):
        raise RuntimeError("Telegram 账号配置无效")
    return replace(
        settings,
        tg_mode="user",
        tg_session=settings.data_dir / "accounts" / key,
        tg_chat_id=profile["chat_id"],
    )


class AccountManager:
    def __init__(self, app, settings: Settings, control: Database, runtime: Runtime):
        self.app, self.settings, self.control, self.current = app, settings, control, runtime
        self.databases = {control.path: control, runtime.db.path: runtime.db}
        self.pending: dict | None = None
        self.lock = asyncio.Lock()
        self.tasks: set[asyncio.Task] = set()
        self.switching = False
        self.identity: dict | None = None
        self.workspace_keys: dict[str, str] = {}
        self.retired: set[Runtime] = set()

    def task(self, coroutine) -> asyncio.Task:
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)

        def finished(result):
            self.tasks.discard(result)
            if not result.cancelled() and result.exception() is not None:
                logger.warning(
                    "Telegram account task failed: %s", type(result.exception()).__name__
                )

        task.add_done_callback(finished)
        return task

    async def start(self) -> None:
        storage = self.current.storage
        if storage.status()["connected"] and hasattr(storage, "me"):
            try:
                self.identity = await asyncio.wait_for(storage.me(), 20)
            except Exception:
                pass
        if storage.chat is not None and not self.control.telegram_profiles():
            identity = self.identity or {}
            self.control.save_telegram_profile(
                {
                    "id": "default",
                    "session_key": "",
                    "chat_id": str(storage.chat),
                    "user_id": identity.get("id", 0),
                    "username": identity.get("username"),
                    "first_name": identity.get("first_name"),
                    "title": "原存储频道",
                },
                "",
            )

    def status(self) -> dict:
        active = self.control.active_telegram_profile()
        return {
            "active_id": active["id"] if active else None,
            "account": self.identity,
            "chat_id": str(self.current.storage.chat or ""),
            "connected": self.current.storage.status()["connected"],
            "can_login": bool(self.settings.tg_api_id and self.settings.tg_api_hash),
            "switching": self.switching,
            "profiles": [
                {k: v for k, v in p.items() if k != "session_key"}
                for p in self.control.telegram_profiles()
            ],
        }

    def login_status(self, job_id: str) -> dict:
        job = self.job(job_id)
        return {key: job.get(key) for key in ("id", "state", "qr_image", "error", "account")}

    def job(self, job_id: str) -> dict:
        if not self.pending or self.pending["id"] != job_id:
            raise HTTPException(404, "登录任务不存在，请重新扫码")
        return self.pending

    async def begin(self) -> dict:
        async with self.lock:
            if not self.settings.tg_api_id or not self.settings.tg_api_hash:
                raise HTTPException(409, "请先配置 TG_API_ID 和 TG_API_HASH")
            at_limit = len(self.control.telegram_profiles()) >= 10
            if at_limit and not self.current.storage.status().get("auth_required"):
                raise HTTPException(409, "最多保存 10 个账号/频道组合，请先使用已有连接")
            await self._cancel()
            key = uuid.uuid4().hex
            directory = self.settings.data_dir / "accounts"
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            client = TelegramClient(
                str(directory / key),
                self.settings.tg_api_id,
                self.settings.tg_api_hash,
                proxy=get_proxy(),
                flood_sleep_threshold=0,
                request_retries=1,
                timeout=20,
            )
            self.pending = {
                "id": key,
                "state": "connecting",
                "client": client,
                "error": None,
                "qr_image": None,
                "account": None,
                "attempts": 0,
                "check_scan": asyncio.Event(),
            }
            self.pending["worker"] = self.task(self._login(self.pending))
            self.pending["expiry"] = self.task(self._expire(self.pending))
            return self.login_status(key)

    def check_scan(self, job_id: str) -> dict:
        job = self.job(job_id)
        if job["state"] == "waiting":
            job.update(state="checking", qr_image=None)
            job["check_scan"].set()
        return self.login_status(job_id)

    async def _login(self, job: dict) -> None:
        client = job["client"]
        check_scan = job["check_scan"]

        async def on_scan(_update) -> None:
            check_scan.set()

        client.add_event_handler(on_scan, events.Raw(types.UpdateLoginToken))
        try:
            await asyncio.wait_for(client.connect(), 30)
            session_file = self.settings.data_dir / "accounts" / f"{job['id']}.session"
            if session_file.exists():
                session_file.chmod(0o600)
            request = functions.auth.ExportLoginTokenRequest(
                self.settings.tg_api_id, self.settings.tg_api_hash, []
            )
            while True:
                check_scan.clear()
                # A Telegram update, a manual check and token expiry all use the
                # same worker so that re-export and DC migration cannot race.
                async with asyncio.timeout(30):
                    result = await client(request)
                    if isinstance(result, types.auth.LoginTokenMigrateTo):
                        await client._switch_dc(result.dc_id)
                        result = await client(functions.auth.ImportLoginTokenRequest(result.token))
                    if isinstance(result, types.auth.LoginTokenSuccess):
                        # Initialize Telethon's update state, as QRLogin.wait does.
                        user = result.authorization.user
                        await client._on_login(user)
                        self._ready(job, user)
                        return
                if not isinstance(result, types.auth.LoginToken):
                    raise RuntimeError("Unexpected Telegram QR login response")
                token = base64.urlsafe_b64encode(result.token).decode().rstrip("=")
                buffer = io.BytesIO()
                qrcode.make(f"tg://login?token={token}").save(buffer, format="PNG")
                job.update(
                    state="waiting",
                    qr_image="data:image/png;base64,"
                    + base64.b64encode(buffer.getvalue()).decode(),
                )
                expires_in = (
                    (result.expires - datetime.now(UTC)).total_seconds() if result.expires else 45
                )
                try:
                    await asyncio.wait_for(check_scan.wait(), max(0.1, expires_in))
                except TimeoutError:
                    pass
                job.update(state="checking", qr_image=None)
        except SessionPasswordNeededError:
            job.update(state="password", qr_image=None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Telegram QR login failed (%s)", type(exc).__name__)
            job.update(state="error", qr_image=None, error="Telegram 登录失败，请检查网络后重试")
        finally:
            client.remove_event_handler(on_scan)

    def _ready(self, job: dict, me) -> None:
        if not me or getattr(me, "bot", False):
            raise HTTPException(400, "请使用个人 Telegram 账号扫码")
        job.update(
            state="ready",
            qr_image=None,
            error=None,
            account={
                "id": me.id,
                "username": getattr(me, "username", None),
                "first_name": getattr(me, "first_name", None),
                "bot": False,
            },
        )

    async def password(self, job_id: str, password: str) -> dict:
        async with self.lock:
            job = self.job(job_id)
            if job["state"] != "password":
                raise HTTPException(409, "当前无需两步验证密码")
            if job["attempts"] >= 5:
                raise HTTPException(429, "验证次数过多，请取消后重新扫码")
            job["attempts"] += 1
            try:
                user = await asyncio.wait_for(job["client"].sign_in(password=password), 30)
                self._ready(job, user)
            except PasswordHashInvalidError:
                raise HTTPException(400, "两步验证密码错误") from None
            except FloodWaitError as exc:
                raise HTTPException(429, f"Telegram 要求等待 {exc.seconds} 秒后重试") from None
            except TimeoutError:
                raise HTTPException(504, "Telegram 验证超时，请重试") from None
            return self.login_status(job_id)

    async def _expire(self, job: dict) -> None:
        await asyncio.sleep(600)
        async with self.lock:
            if self.pending is job:
                await self._cancel(expired=True)

    async def _cancel(self, expired: bool = False) -> None:
        job = self.pending
        if not job:
            return
        current = asyncio.current_task()
        for name in ("worker", "expiry"):
            task = job.get(name)
            if task and task is not current:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        try:
            authorized = bool(job.get("account"))
            if not authorized:
                try:
                    authorized = await asyncio.wait_for(job["client"].is_user_authorized(), 5)
                except Exception:
                    pass
            if authorized:
                try:
                    await asyncio.wait_for(job["client"].log_out(), 10)
                except Exception:
                    pass  # Best effort revoke of an unused newly-authorized session.
            await asyncio.wait_for(job["client"].disconnect(), 10)
        finally:
            for suffix in (".session", ".session-journal", ".session-wal", ".session-shm"):
                (self.settings.data_dir / "accounts" / (job["id"] + suffix)).unlink(missing_ok=True)
            job.update(
                state="expired" if expired else "canceled",
                qr_image=None,
                error="登录已过期，请重新扫码" if expired else None,
            )
            self.pending = job if expired else None

    async def cancel(self, job_id: str) -> None:
        async with self.lock:
            self.job(job_id)
            await self._cancel()

    @staticmethod
    def writable(entity) -> bool:
        rights = getattr(entity, "admin_rights", None)
        return isinstance(entity, Channel) and (
            bool(getattr(entity, "creator", False))
            or bool(
                rights and rights.delete_messages and (not entity.broadcast or rights.post_messages)
            )
        )

    async def channels(self, job_id: str) -> list[dict]:
        async with self.lock:
            job = self.job(job_id)
            if job["state"] != "ready":
                raise HTTPException(409, "请先完成扫码和两步验证")
            result = []
            try:
                async with asyncio.timeout(30):
                    async for dialog in job["client"].iter_dialogs(limit=200):
                        if isinstance(dialog.entity, Channel):
                            result.append(
                                {
                                    "id": str(utils.get_peer_id(dialog.entity)),
                                    "title": dialog.name,
                                    "writable": self.writable(dialog.entity),
                                }
                            )
            except Exception:
                raise HTTPException(502, "读取频道失败，请检查连接后重试") from None
            return result

    async def _entity(self, client, target: str):
        target = TelegramStorage._normalize_chat(target)
        try:
            entity = await client.get_entity(target)
        except ValueError:
            entity = None
            async for dialog in client.iter_dialogs(limit=None):
                if utils.get_peer_id(dialog.entity) == target:
                    entity = dialog.entity
                    break
        if not entity or not self.writable(entity):
            raise HTTPException(400, "请选择已加入的频道，并授予新账号发送和删除消息的管理员权限")
        return entity

    async def activate_login(self, job_id: str, chat_id: str) -> dict:
        async with self.lock:
            job = self.job(job_id)
            if job["state"] != "ready":
                raise HTTPException(409, "请先完成扫码和两步验证")
            try:
                entity = await asyncio.wait_for(self._entity(job["client"], chat_id), 45)
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(502, "无法验证频道，请检查频道 ID 和账号权限") from None
            account = job["account"]
            target_chat_id = str(utils.get_peer_id(entity))
            active = self.control.active_telegram_profile()
            replace_active = (
                active is not None
                and self.current.storage.status().get("auth_required")
                and active["chat_id"] == target_chat_id
                and active["user_id"] in {0, account["id"]}
            )
            if not replace_active and len(self.control.telegram_profiles()) >= 10:
                raise HTTPException(
                    409, "已达到连接数量上限，请使用原 Telegram 账号并选择原存储频道"
                )
            profile = {
                "id": active["id"] if replace_active else job_id,
                "session_key": job_id,
                "chat_id": target_chat_id,
                "user_id": account["id"],
                "username": account["username"],
                "first_name": account["first_name"],
                "title": entity.title,
            }
            candidate = TelegramStorage(account_settings(self.settings, profile))
            candidate.client, candidate.chat, candidate._started = (
                job["client"],
                int(profile["chat_id"]),
                True,
            )
            await self._activate(candidate, profile)
            for name in ("worker", "expiry"):
                task = job.get(name)
                if task:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            self.pending = None  # The saved client/session now belongs to the active runtime.
            return self.status()

    async def activate_saved(self, profile_id: str) -> dict:
        async with self.lock:
            profile = next(
                (p for p in self.control.telegram_profiles() if p["id"] == profile_id), None
            )
            if not profile:
                raise HTTPException(404, "保存的连接不存在")
            active = self.control.active_telegram_profile()
            if active and active["id"] == profile_id:
                storage_status = self.current.storage.status()
                if storage_status["connected"]:
                    return self.status()
                if storage_status.get("auth_required"):
                    raise HTTPException(409, storage_status["error"])
            settings = account_settings(self.settings, profile)
            if any(
                getattr(runtime.storage, "settings", None)
                and runtime.storage.settings.tg_session == settings.tg_session
                for runtime in self.retired
            ):
                raise HTTPException(409, "该连接仍有旧请求正在结束，请等待下载完成后重试")
            candidate = TelegramStorage(settings)
            try:
                await asyncio.wait_for(candidate.start(), 45)
            except TimeoutError:
                await candidate.stop()
                raise HTTPException(504, "Telegram 连接超时，请稍后重试") from None
            if not candidate.status()["connected"]:
                await candidate.stop()
                raise HTTPException(502, "保存的账号连接失败，请重新扫码登录")
            try:
                entity = await asyncio.wait_for(
                    self._entity(candidate.client, profile["chat_id"]), 45
                )
                if str(utils.get_peer_id(entity)) != profile["chat_id"]:
                    raise HTTPException(409, "频道配置已变化，请重新扫码选择频道")
                await self._activate(candidate, profile)
            except BaseException:
                await candidate.stop()
                raise
            return self.status()

    def workspace(self, chat_id: str) -> tuple[Database, str]:
        key = self.control.telegram_workspace(chat_id)
        if key == "":
            return self.control, ""
        key = key or self.workspace_keys.setdefault(chat_id, uuid.uuid4().hex)
        if not re.fullmatch(r"[a-f0-9]{32}", key):
            raise RuntimeError("频道数据库配置无效")
        directory = self.settings.data_dir / "channels"
        directory.mkdir(parents=True, exist_ok=True)
        path = str(directory / f"{key}.sqlite3")
        if path not in self.databases:
            self.databases[path] = Database(path)
        return self.databases[path], key

    async def _activate(self, candidate: TelegramStorage, profile: dict) -> None:
        old = self.current
        if old.uploads.active_count():
            raise HTTPException(409, "当前仍有文件正在写入 Telegram，请等待完成后切换")
        self.switching = True
        old.uploads._closed = True
        candidate.traffic = self.app.state.traffic
        new = None
        try:
            db, key = self.workspace(profile["chat_id"])
            new = Runtime(
                db,
                candidate,
                UploadService(
                    db,
                    candidate,
                    self.settings.upload_stale_hours,
                    self.settings.upload_concurrency,
                ),
                ChannelSyncService(db, candidate, self.settings),
            )
            await old.sync.shutdown()
            db.recover_interrupted_uploads()
            await asyncio.wait_for(new.sync.start(), 30)
            self.control.save_telegram_profile(profile, key)
        except BaseException:
            if new:
                await new.sync.shutdown()
            old.uploads._closed = False
            old.sync._full = True
            if old.sync.state == "stopped":
                await old.sync.start()
            self.switching = False
            raise
        new.uploads.start()
        self.current = new
        self.app.state.db, self.app.state.storage = db, candidate
        self.app.state.uploads, self.app.state.channel_sync = new.uploads, new.sync
        self.identity = {
            "id": profile["user_id"],
            "username": profile["username"],
            "first_name": profile["first_name"],
            "bot": candidate.mode == "bot",
        }
        old.retired = True
        self.retired.add(old)
        self.switching = False
        if old.leases == 0:
            self.task(self._dispose(old))

    async def _dispose(self, runtime: Runtime) -> None:
        await runtime.uploads.shutdown()
        await runtime.storage.stop()
        self.retired.discard(runtime)

    async def shutdown(self) -> None:
        async with self.lock:
            await self._cancel()
        await self.current.sync.shutdown()
        await self.current.uploads.shutdown()
        await self.current.storage.stop()
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
        for runtime in list(self.retired):
            await self._dispose(runtime)
        self.app.state.traffic.flush()
        for db in self.databases.values():
            db.close()


class WorkspaceMiddleware:
    def __init__(self, app, manager: AccountManager):
        self.app, self.manager = app, manager

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        runtime = self.manager.current
        runtime.leases += 1
        scope.setdefault("state", {})["telegram_runtime"] = runtime
        try:
            headers = dict(scope.get("headers", []))
            query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
            expected = (
                headers.get(b"x-workspace-id", b"").decode("latin-1")
                or query.get("workspace_id", [""])[0]
            )
            if (
                scope["path"].startswith(
                    ("/api/items", "/api/files", "/api/uploads", "/api/folders")
                )
                and scope.get("session", {}).get("auth")
                and expected
                and expected != str(runtime.storage.chat or "")
            ):
                return await JSONResponse(
                    {"detail": "存储频道已切换，请刷新页面", "workspace_changed": True},
                    status_code=409,
                )(scope, receive, send)
            await self.app(scope, receive, send)
        finally:
            runtime.leases -= 1
            if runtime.retired and runtime.leases == 0:
                self.manager.task(self.manager._dispose(runtime))
