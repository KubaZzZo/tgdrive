from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.deps import get_settings
from app.auth import check_password
from app.config import Settings
from app.schemas import LoginBody

router = APIRouter(prefix="/api", tags=["auth"])


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.post("/login")
async def login(
    body: LoginBody,
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict[str, bool]:
    limiter = request.app.state.login_limiter
    client_key = _client_key(request)
    retry_after = limiter.retry_after(client_key)
    if retry_after:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"登录尝试过于频繁，请 {retry_after} 秒后再试",
            headers={"Retry-After": str(retry_after)},
        )
    if not check_password(body.password, settings.app_password):
        limiter.record_failure(client_key)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="密码错误")
    limiter.reset(client_key)
    request.session.clear()
    request.session["auth"] = True
    return {"ok": True}


@router.post("/logout")
async def logout(request: Request) -> dict[str, bool]:
    request.session.clear()
    return {"ok": True}
