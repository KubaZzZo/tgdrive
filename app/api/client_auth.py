from __future__ import annotations

import re

from fastapi import Depends, HTTPException
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBasic,
    HTTPBasicCredentials,
    HTTPBearer,
)

from app.api.deps import get_db, get_storage
from app.infrastructure.db import Database
from app.infrastructure.storage import TelegramStorage

_basic = HTTPBasic(auto_error=False, realm="TG Drive")
_bearer = HTTPBearer(auto_error=False)
WEBDAV_USERNAME = "tgdrive"


def require_channel(storage: TelegramStorage) -> str:
    if storage.chat is None:
        raise HTTPException(409, "请先配置 Telegram 存储频道")
    return str(storage.chat)


def _authorized(db: Database, storage: TelegramStorage, scope: str, secret: str) -> bool:
    return bool(
        storage.chat is not None
        and re.fullmatch(r"[A-Za-z0-9_-]{43}", secret)
        and db.accepts_client_credential(str(storage.chat), scope, secret)
    )


async def require_picgo(
    auth: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Database = Depends(get_db),
    storage: TelegramStorage = Depends(get_storage),
) -> None:
    if not auth or not _authorized(db, storage, "picgo", auth.credentials):
        raise HTTPException(401, "PicGo 令牌无效", headers={"WWW-Authenticate": "Bearer"})


async def require_webdav(
    auth: HTTPBasicCredentials | None = Depends(_basic),
    db: Database = Depends(get_db),
    storage: TelegramStorage = Depends(get_storage),
) -> None:
    if (
        not auth
        or auth.username != WEBDAV_USERNAME
        or not _authorized(db, storage, "webdav", auth.password)
    ):
        raise HTTPException(
            401, "WebDAV 应用密码无效", headers={"WWW-Authenticate": 'Basic realm="TG Drive"'}
        )
