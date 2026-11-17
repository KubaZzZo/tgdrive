from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from telethon import TelegramClient, utils

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import settings  # noqa: E402
from app.core.network import get_proxy  # noqa: E402


async def main() -> int:
    if not settings.tg_api_id or not settings.tg_api_hash:
        print("缺少 TG_API_ID / TG_API_HASH")
        return 2
    settings.ensure_dirs()
    settings.tg_session.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(settings.tg_session), settings.tg_api_id, settings.tg_api_hash, proxy=get_proxy())
    await client.connect()
    try:
        if not await client.is_user_authorized():
            print("session 未登录，请先运行: python scripts/login_qr.py")
            return 2
        print("把下面目标频道的 ID 填到 .env 的 TG_CHAT_ID：")
        print("-" * 72)
        async for dialog in client.iter_dialogs(limit=200):
            entity = dialog.entity
            username = getattr(entity, "username", None)
            kind = type(entity).__name__
            print(f"{utils.get_peer_id(entity):>16}  {kind:<24} {dialog.name}" + (f"  @{username}" if username else ""))
        return 0
    finally:
        await client.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
