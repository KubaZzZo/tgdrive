from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import qrcode
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

# Allow `python scripts/login_qr.py` from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.network import get_proxy  # noqa: E402


async def main() -> int:
    if error := settings.telegram_login_error:
        print(error)
        return 2

    session_path = str(settings.tg_session)
    client = TelegramClient(session_path, settings.tg_api_id, settings.tg_api_hash, proxy=get_proxy())
    await client.connect()
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"已经登录: {getattr(me, 'username', None) or me.id}")
            return 0

        qr = await client.qr_login()
        qr_path = settings.data_dir / "login_qr.png"
        print("请用手机 Telegram 扫描二维码：")
        print(f"二维码文件: {qr_path}")
        print("等待扫码中（二维码过期会自动刷新）...")

        while True:
            image = qrcode.make(qr.url)
            tmp = qr_path.with_suffix(".tmp.png")
            image.save(tmp)
            tmp.replace(qr_path)
            print(f"当前登录链接: {qr.url}")
            try:
                await qr.wait(timeout=45)
                break
            except asyncio.TimeoutError:
                print("二维码已过期，正在刷新...")
                try:
                    await asyncio.wait_for(qr.recreate(), timeout=25)
                except Exception as exc:
                    print(f"刷新二维码失败: {exc}")
                    try:
                        await client.disconnect()
                        await client.connect()
                        qr = await client.qr_login()
                    except Exception as reconnect_exc:
                        print(f"重新连接失败: {reconnect_exc}")
                        return 4
    except SessionPasswordNeededError:
        password = os.getenv("TG_2FA_PASSWORD", "").strip()
        password_file = settings.data_dir / "2fa.txt"
        if not password:
            print("该账号启用了两步验证。")
            print(f"请把 2FA 密码写入: {password_file}")
            print("检测到密码后会自动继续，登录成功后该文件会被删除。")
            for _ in range(300):
                if password_file.exists():
                    password = password_file.read_text(encoding="utf-8").strip()
                    break
                await asyncio.sleep(1)
        if not password:
            print("等待 2FA 密码超时。")
            return 3
        await client.sign_in(password=password)
        password_file.unlink(missing_ok=True)
        me = await client.get_me()
        print(f"登录成功: {getattr(me, 'username', None) or me.id}")
        print(f"session 已保存到: {session_path}.session")
        return 0
    finally:
        await client.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
