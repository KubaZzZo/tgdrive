from __future__ import annotations

import asyncio
import logging

from telethon.tl.types import Channel

from app.services.accounts import AccountManager, Runtime
from app.services.telegram_bot import BotAPIError

logger = logging.getLogger("tgdrive.diagnostics")


def test_channel(manager: AccountManager, runtime: Runtime) -> int | None:
    target = manager.settings.tg_diagnostic_chat_id
    current = runtime.storage.chat
    if not target or not runtime.sync.bot or not isinstance(current, int):
        return None
    configured = manager.settings.tg_chat_id
    # With a different active profile, an unresolved default channel alias is ambiguous.
    if (
        configured
        and not configured.lstrip("-").isdigit()
        and manager.control.active_telegram_profile()
    ):
        return None
    if target == current or str(target) == configured:
        return None
    if manager.control.telegram_workspace(str(target)) is not None:
        return None
    return target


async def inspect_telegram(manager: AccountManager, runtime: Runtime) -> dict:
    storage, sync = runtime.storage, runtime.sync

    async def session() -> dict:
        info = storage.status()
        result = {
            "state": "unavailable",
            "mode": info.get("mode"),
            "can_post": None,
            "can_delete": None,
        }
        client = getattr(storage, "client", None)
        if info.get("auth_required"):
            return {**result, "state": "auth_required"}
        if not info["connected"] or client is None:
            return result
        try:
            async with asyncio.timeout(8):
                me = await client.get_me()
                if me is None:
                    return {**result, "state": "auth_required"}
                result["state"] = "connected"
                entity = await client.get_entity(storage.chat)
                if isinstance(entity, Channel):
                    rights = entity.admin_rights
                    result["can_delete"] = bool(
                        entity.creator or (rights and rights.delete_messages)
                    )
                    result["can_post"] = bool(
                        entity.creator
                        or (rights and (not entity.broadcast or rights.post_messages))
                        or (
                            not entity.broadcast
                            and not entity.left
                            and not (
                                getattr(entity.banned_rights, "send_messages", False)
                                or getattr(entity.default_banned_rights, "send_messages", False)
                            )
                        )
                    )
        except Exception as exc:
            logger.info("Telegram session probe did not finish (%s)", type(exc).__name__)
            if result["state"] != "connected":
                result["state"] = "error"
            result["error"] = "无法读取会话或频道权限"
        return result

    async def fallback() -> dict:
        result = {"state": "not_configured", "can_post": None, "can_delete": None}
        if sync.bot is None:
            return result
        try:
            async with asyncio.timeout(8):
                me = await sync.bot.call("getMe")
                result["state"] = "connected"
                if isinstance(storage.chat, int):
                    member = await sync.bot.call(
                        "getChatMember", chat_id=storage.chat, user_id=me["id"]
                    )
                    owner = member.get("status") == "creator"
                    result["can_post"] = owner or bool(member.get("can_post_messages"))
                    result["can_delete"] = owner or bool(member.get("can_delete_messages"))
        except BotAPIError as exc:
            return {**result, "state": "error", "error": str(exc)}
        except (TimeoutError, KeyError, TypeError):
            return {**result, "state": "error", "error": "无法查询备用 Bot"}
        return result

    personal, bot = await asyncio.gather(session(), fallback())
    return {
        "channel_id": str(storage.chat) if storage.chat is not None else None,
        "session": personal,
        "bot": bot,
        "sync": {
            "state": sync.state,
            "error": sync.last_error,
            "last_success": sync.last_success,
            "bot_state": sync.bot_state,
            "bot_error": sync.bot_error,
        },
        "test_channel_id": str(target) if (target := test_channel(manager, runtime)) else None,
    }
