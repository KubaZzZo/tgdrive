from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")


def _as_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return int(value)


def _as_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "TG Drive")
    host: str = os.getenv("HOST", "127.0.0.1")
    port: int = _as_int("PORT", 8787)

    data_dir: Path = Path(os.getenv("DATA_DIR", str(ROOT_DIR / "data")))
    temp_dir: Path = Path(os.getenv("TEMP_DIR", str(ROOT_DIR / "tmp")))
    db_path: Path = Path(os.getenv("DB_PATH", str(ROOT_DIR / "data" / "tgdrive.sqlite3")))

    app_password: str = os.getenv("APP_PASSWORD", "change-me")
    app_secret: str = os.getenv("APP_SECRET", "change-this-secret-in-env")
    cookie_secure: bool = _as_bool("COOKIE_SECURE", False)

    tg_mode: str = os.getenv("TG_MODE", "user").strip().lower()
    tg_api_id: int = _as_int("TG_API_ID", 0)
    tg_api_hash: str = os.getenv("TG_API_HASH", "").strip()
    tg_bot_token: str = os.getenv("TG_BOT_TOKEN", "").strip()
    tg_session: Path = Path(os.getenv("TG_SESSION", str(ROOT_DIR / "data" / "tgdrive")))
    tg_chat_id: str = os.getenv("TG_CHAT_ID", "").strip()
    tg_proxy: str = os.getenv("TG_PROXY", "").strip()

    # Telegram hard limit is 2 GB per file for normal accounts.
    # Keeping a little headroom avoids edge cases at exactly 2 GB.
    tg_segment_size: int = _as_int("TG_SEGMENT_SIZE", 1_800_000_000)
    upload_chunk_size: int = _as_int("UPLOAD_CHUNK_SIZE", 8 * 1024 * 1024)
    max_file_size: int = _as_int("MAX_FILE_SIZE", 0)  # 0 = unlimited

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def is_configured(self) -> bool:
        if self.tg_mode == "bot":
            return bool(self.tg_api_id and self.tg_api_hash and self.tg_bot_token and self.tg_chat_id)
        return bool(self.tg_api_id and self.tg_api_hash and self.tg_chat_id)

    @property
    def config_hint(self) -> str:
        if self.tg_mode == "bot":
            return "请在 .env 配置 TG_API_ID、TG_API_HASH、TG_BOT_TOKEN、TG_CHAT_ID"
        return "请在 .env 配置 TG_API_ID、TG_API_HASH、TG_CHAT_ID，并运行 scripts/login_qr.py"


settings = Settings()
settings.ensure_dirs()
