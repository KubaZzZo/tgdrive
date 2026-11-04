from __future__ import annotations

import hmac

from fastapi import HTTPException, Request, status


def check_password(password: str, expected: str) -> bool:
    return hmac.compare_digest((password or "").encode("utf-8"), (expected or "").encode("utf-8"))


def is_authenticated(request: Request) -> bool:
    return bool(request.session.get("auth"))


def require_auth(request: Request) -> None:
    if not is_authenticated(request):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
