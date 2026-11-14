from fastapi import APIRouter, Depends, Request

from app.api.schemas.accounts import ChannelBody, PasswordBody
from app.core.security import require_auth

router = APIRouter(prefix="/api/telegram", tags=["telegram"], dependencies=[Depends(require_auth)])


@router.get("")
async def status(request: Request):
    return request.app.state.accounts.status()


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
