from __future__ import annotations

import hmac
from math import ceil
from time import time
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request, status

if TYPE_CHECKING:
    from app.infrastructure.db import Database


class LoginRateLimiter:
    """Sliding-window limiter persisted in the installation's control database."""

    def __init__(self, max_attempts: int, window_seconds: int, db: Database):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.db = db

    def retry_after(self, key: str) -> int:
        now = time()
        count, first = self.db.login_failure_window(key, now - self.window_seconds)
        if count < self.max_attempts or first is None:
            return 0
        return max(1, ceil(self.window_seconds - (now - first)))

    def record_failure(self, key: str) -> None:
        now = time()
        self.db.record_login_failure(key, now, now - self.window_seconds)

    def reset(self, key: str) -> None:
        self.db.reset_login_failures(key)


def check_password(password: str, expected: str) -> bool:
    return hmac.compare_digest((password or "").encode("utf-8"), (expected or "").encode("utf-8"))


def is_authenticated(request: Request) -> bool:
    return bool(request.session.get("auth"))


def require_auth(request: Request) -> None:
    if not is_authenticated(request):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
