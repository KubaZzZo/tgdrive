from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth import require_auth

router = APIRouter(prefix="/api/telegram", tags=["telegram"], dependencies=[Depends(require_auth)])


class PasswordBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=1, max_length=512)


class ChannelBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    chat_id: str = Field(min_length=1, max_length=128)


@router.get("")
async def status(request: Request):
    return request.app.state.accounts.status()


@router.post("/login", status_code=202)
async def begin(request: Request):
    return await request.app.state.accounts.begin()


@router.get("/login/{job_id}")
async def progress(job_id: str, request: Request):
    return request.app.state.accounts.login_status(job_id)


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
