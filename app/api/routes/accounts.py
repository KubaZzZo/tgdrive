import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.schemas.accounts import ChannelBody, PasswordBody
from app.core.security import require_auth
from app.services.telegram_bot import BotAPIError
from app.services.telegram_diagnostics import inspect_telegram, test_channel

router = APIRouter(prefix="/api/telegram", tags=["telegram"], dependencies=[Depends(require_auth)])


@router.get("")
async def status(request: Request):
    return request.app.state.accounts.status()


@router.get("/diagnostics")
async def diagnostics(request: Request):
    manager = request.app.state.accounts
    runtime = request.state.telegram_runtime
    if manager.current is not runtime:
        raise HTTPException(409, "频道已切换，请刷新后重试")
    report = await inspect_telegram(manager, runtime)
    if manager.current is not runtime:
        raise HTTPException(409, "频道已切换，请刷新后重试")
    return report


@router.post("/diagnostics/test-message")
async def send_diagnostic_message(body: ChannelBody, request: Request):
    manager = request.app.state.accounts
    runtime = request.state.telegram_runtime
    async with manager.lock:
        target = test_channel(manager, runtime)
        bot = runtime.sync.bot
        if manager.current is not runtime or not target or bot is None:
            raise HTTPException(409, "请配置与存储频道独立的专用测试频道和备用 Bot")
        if str(target) != body.chat_id:
            raise HTTPException(409, "测试频道已变化，请重新诊断后确认")
        try:
            async with asyncio.timeout(10):
                channel = await bot.call("getChat", chat_id=target)
                if channel.get("type") != "channel" or channel.get("id") != target:
                    raise HTTPException(409, "配置的目标必须是专用 Telegram 频道")
                await bot.call("sendMessage", chat_id=target, text="TG Drive 备用 Bot 手动诊断")
        except (TimeoutError, BotAPIError):
            raise HTTPException(502, "测试消息发送失败或超时，请检查 Bot 和测试频道权限") from None
    return {"channel_id": str(target)}


@router.post("/login", status_code=202)
async def begin(request: Request):
    return await request.app.state.accounts.begin()


@router.get("/login/{job_id}")
async def progress(job_id: str, request: Request):
    return request.app.state.accounts.login_status(job_id)


@router.post("/login/{job_id}/check", status_code=202)
async def check_scan(job_id: str, request: Request):
    return request.app.state.accounts.check_scan(job_id)


@router.delete("/login/{job_id}")
async def cancel(job_id: str, request: Request):
    await request.app.state.accounts.cancel(job_id)
    return {"ok": True}


@router.post("/login/{job_id}/password")
async def password(job_id: str, body: PasswordBody, request: Request):
    return await request.app.state.accounts.password(job_id, body.password)


@router.get("/login/{job_id}/channels")
async def channels(job_id: str, request: Request):
    return {"channels": await request.app.state.accounts.channels(job_id)}


@router.post("/login/{job_id}/activate")
async def activate(job_id: str, body: ChannelBody, request: Request):
    return await request.app.state.accounts.activate_login(job_id, body.chat_id)


@router.post("/profiles/{profile_id}/activate")
async def activate_profile(profile_id: str, request: Request):
    return await request.app.state.accounts.activate_saved(profile_id)


@router.delete("/profiles/{profile_id}")
async def delete_profile(profile_id: str, request: Request):
    return await request.app.state.accounts.delete_saved(profile_id)
