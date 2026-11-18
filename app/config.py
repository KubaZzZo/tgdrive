from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")
logger = logging.getLogger("tgdrive.config")


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"环境变量 {name} 必须是整数") from exc


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_path(name: str, default: str) -> Path:
    path = Path(os.getenv(name, default)).expanduser()
    if not path.is_absolute():
        path = ROOT_DIR / path
    return path.resolve()


@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str = field(default_factory=lambda: os.getenv("APP_NAME", "TG Drive"))
    app_env: str = field(
        default_factory=lambda: os.getenv("APP_ENV", "development").strip().lower()
    )
    host: str = field(default_factory=lambda: os.getenv("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("PORT", 8787))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO").upper())
    log_json: bool = field(default_factory=lambda: _env_bool("LOG_JSON", False))

    data_dir: Path = field(default_factory=lambda: _env_path("DATA_DIR", "data"))
    temp_dir: Path = field(default_factory=lambda: _env_path("TEMP_DIR", "tmp"))
    db_path: Path = field(default_factory=lambda: _env_path("DB_PATH", "data/tgdrive.sqlite3"))
    frontend_dist: Path = field(default_factory=lambda: _env_path("FRONTEND_DIST", "static/dist"))
    people_enabled: bool = field(default_factory=lambda: _env_bool("PEOPLE_ENABLED", True))
    image_host_enabled: bool = field(default_factory=lambda: _env_bool("IMAGE_HOST_ENABLED", True))
    traffic_enabled: bool = field(default_factory=lambda: _env_bool("TRAFFIC_ENABLED", True))
    google_drive_dav_url: str = field(
        default_factory=lambda: os.getenv("GOOGLE_DRIVE_DAV_URL", "").strip()
    )
    google_drive_dav_username: str = field(
        default_factory=lambda: os.getenv("GOOGLE_DRIVE_DAV_USERNAME", "").strip()
    )
    google_drive_dav_password: str = field(
        default_factory=lambda: os.getenv("GOOGLE_DRIVE_DAV_PASSWORD", ""), repr=False
    )
    axisnow_api_token: str = field(
        default_factory=lambda: os.getenv("AXISNOW_API_TOKEN", "").strip(), repr=False
    )
    axisnow_subscription_ids: str = field(
        default_factory=lambda: os.getenv("AXISNOW_SUBSCRIPTION_IDS", "").strip()
    )
    cdn_warning_percent: int = field(default_factory=lambda: _env_int("CDN_WARNING_PERCENT", 80))
    cdn_stop_percent: int = field(default_factory=lambda: _env_int("CDN_STOP_PERCENT", 95))
    media_cache_dir: Path | None = field(
        default_factory=lambda: (
            _env_path("MEDIA_CACHE_DIR", "") if os.getenv("MEDIA_CACHE_DIR", "").strip() else None
        )
    )
    media_cache_max_mib: int = field(default_factory=lambda: _env_int("MEDIA_CACHE_MAX_MIB", 512))
    media_cache_min_free_mib: int = field(
        default_factory=lambda: _env_int("MEDIA_CACHE_MIN_FREE_MIB", 4096)
    )
    media_cache_slice_mib: int = field(default_factory=lambda: _env_int("MEDIA_CACHE_SLICE_MIB", 2))
    media_cache_ttl_seconds: int = field(
        default_factory=lambda: _env_int("MEDIA_CACHE_TTL_SECONDS", 600)
    )

    app_password: str = field(default_factory=lambda: os.getenv("APP_PASSWORD", "change-me"))
    app_secret: str = field(
        default_factory=lambda: os.getenv("APP_SECRET", "change-this-secret-in-env")
    )
    cookie_secure: bool = field(default_factory=lambda: _env_bool("COOKIE_SECURE", False))
    session_max_age: int = field(
        default_factory=lambda: _env_int("SESSION_MAX_AGE", 14 * 24 * 3600)
    )

    login_max_attempts: int = field(default_factory=lambda: _env_int("LOGIN_MAX_ATTEMPTS", 5))
    login_window_seconds: int = field(default_factory=lambda: _env_int("LOGIN_WINDOW_SECONDS", 300))

    tg_mode: str = field(default_factory=lambda: os.getenv("TG_MODE", "user").strip().lower())
    tg_api_id: int = field(default_factory=lambda: _env_int("TG_API_ID", 0))
    tg_api_hash: str = field(default_factory=lambda: os.getenv("TG_API_HASH", "").strip())
    tg_bot_token: str = field(default_factory=lambda: os.getenv("TG_BOT_TOKEN", "").strip())
    tg_bot_fallback_token: str = field(
        default_factory=lambda: os.getenv("TG_BOT_FALLBACK_TOKEN", "").strip(), repr=False
    )
    tg_diagnostic_chat_id: int = field(default_factory=lambda: _env_int("TG_DIAGNOSTIC_CHAT_ID", 0))
    tg_session: Path = field(default_factory=lambda: _env_path("TG_SESSION", "data/tgdrive"))
    tg_chat_id: str = field(default_factory=lambda: os.getenv("TG_CHAT_ID", "").strip())
    tg_proxy: str = field(default_factory=lambda: os.getenv("TG_PROXY", "").strip())

    # Telegram hard limit is 2 GB per file for normal accounts.
    tg_segment_size: int = field(default_factory=lambda: _env_int("TG_SEGMENT_SIZE", 1_800_000_000))
    tg_upload_connections: int = field(default_factory=lambda: _env_int("TG_UPLOAD_CONNECTIONS", 4))
    upload_chunk_size: int = field(
        default_factory=lambda: _env_int("UPLOAD_CHUNK_SIZE", 8 * 1024 * 1024)
    )
    upload_concurrency: int = field(default_factory=lambda: _env_int("UPLOAD_CONCURRENCY", 2))
    max_file_size: int = field(
        default_factory=lambda: _env_int("MAX_FILE_SIZE", 0)
    )  # 0 = unlimited
    upload_stale_hours: int = field(default_factory=lambda: _env_int("UPLOAD_STALE_HOURS", 72))
    video_import_dir: Path | None = field(
        default_factory=lambda: (
            _env_path("VIDEO_IMPORT_DIR", "") if os.getenv("VIDEO_IMPORT_DIR", "").strip() else None
        )
    )
    video_import_ed2k_seeds: Path | None = field(
        default_factory=lambda: (
            _env_path("VIDEO_IMPORT_ED2K_SEEDS", "")
            if os.getenv("VIDEO_IMPORT_ED2K_SEEDS", "").strip()
            else None
        )
    )
    video_import_ed2k_port: int = field(
        default_factory=lambda: _env_int("VIDEO_IMPORT_ED2K_PORT", 0)
    )
    video_import_max_mib: int = field(
        default_factory=lambda: _env_int("VIDEO_IMPORT_MAX_MIB", 8192)
    )
    video_import_qb_url: str = field(
        default_factory=lambda: os.getenv("VIDEO_IMPORT_QB_URL", "").strip()
    )
    video_import_qb_username: str = field(
        default_factory=lambda: os.getenv("VIDEO_IMPORT_QB_USERNAME", "admin").strip()
    )
    video_import_qb_password: str = field(
        default_factory=lambda: os.getenv("VIDEO_IMPORT_QB_PASSWORD", ""), repr=False
    )
    upload_temp_max_mib: int = field(default_factory=lambda: _env_int("UPLOAD_TEMP_MAX_MIB", 51200))
    upload_temp_min_free_mib: int = field(
        default_factory=lambda: _env_int("UPLOAD_TEMP_MIN_FREE_MIB", 512)
    )
    video_faststart: bool = field(default_factory=lambda: _env_bool("VIDEO_FASTSTART", True))
    video_faststart_timeout: int = field(
        default_factory=lambda: _env_int("VIDEO_FASTSTART_TIMEOUT", 300)
    )

    channel_sync_enabled: bool = field(
        default_factory=lambda: _env_bool("CHANNEL_SYNC_ENABLED", True)
    )
    channel_sync_interval: int = field(
        default_factory=lambda: _env_int("CHANNEL_SYNC_INTERVAL", 30)
    )
    people_ai_base_url: str = field(
        default_factory=lambda: os.getenv("PEOPLE_AI_BASE_URL", "").strip()
    )
    people_ai_model: str = field(default_factory=lambda: os.getenv("PEOPLE_AI_MODEL", "").strip())
    people_ai_api_key: str = field(
        default_factory=lambda: os.getenv("PEOPLE_AI_API_KEY", "").strip(), repr=False
    )

    @property
    def google_drive_enabled(self) -> bool:
        return bool(self.google_drive_dav_url)

    @property
    def is_production(self) -> bool:
        return self.app_env in {"prod", "production"}

    @property
    def is_configured(self) -> bool:
        if self.tg_mode == "bot":
            return bool(
                self.tg_api_id and self.tg_api_hash and self.tg_bot_token and self.tg_chat_id
            )
        return bool(self.tg_api_id and self.tg_api_hash and self.tg_chat_id)

    @property
    def config_hint(self) -> str:
        if self.tg_mode == "bot":
            return "请在 .env 配置 TG_API_ID、TG_API_HASH、TG_BOT_TOKEN、TG_CHAT_ID"
        return "请在 .env 配置 TG_API_ID、TG_API_HASH、TG_CHAT_ID，并运行 scripts/login_qr.py"

    @property
    def telegram_login_error(self) -> str | None:
        if self.tg_api_id in {4, 6}:
            return (
                "当前使用 Telegram Android 的共享 API ID。请在 my.telegram.org 申请自己的 "
                "API ID 和 API Hash，更新 .env 中的 TG_API_ID 和 TG_API_HASH 后重启应用。"
            )
        if self.tg_api_id <= 0 or not self.tg_api_hash:
            return "请先在 .env 配置 TG_API_ID 和 TG_API_HASH 后重启应用。"
        return None

    @property
    def index_file(self) -> Path:
        return self.frontend_dist / "index.html"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def validate(self) -> None:
        if self.app_password == "change-me":
            logger.warning("APP_PASSWORD uses the insecure default; set a private password")
        if self.app_secret == "change-this-secret-in-env":
            logger.warning("APP_SECRET uses the insecure default; set a random secret")
        if self.google_drive_enabled:
            url = urlsplit(self.google_drive_dav_url)
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or "@" in url.netloc
                or url.query
                or url.fragment
                or not url.path.strip("/")
            ):
                raise RuntimeError("GOOGLE_DRIVE_DAV_URL 必须是完整的 WebDAV 目录地址")
            if not self.google_drive_dav_username or not self.google_drive_dav_password:
                raise RuntimeError("Google Drive 挂载需要配置只读 WebDAV 用户名和密码")
        if self.tg_diagnostic_chat_id and self.tg_diagnostic_chat_id >= -1_000_000_000_000:
            raise RuntimeError("TG_DIAGNOSTIC_CHAT_ID 必须是专用 Telegram 频道的 -100… 数字 ID")
        if self.video_import_max_mib <= 0:
            raise RuntimeError("VIDEO_IMPORT_MAX_MIB 必须大于 0")
        # aMule also binds UDP at TCP + 10, which must fit in the port range.
        if self.video_import_ed2k_port != 0 and not 1024 <= self.video_import_ed2k_port <= 65525:
            raise RuntimeError("VIDEO_IMPORT_ED2K_PORT 必须为 0 或 1024 至 65525")
        if self.video_import_qb_url and (
            not self.video_import_qb_username or not self.video_import_qb_password
        ):
            raise RuntimeError("qBittorrent 导入需要配置 Web UI 用户名和密码")
        if self.video_faststart_timeout <= 0:
            raise RuntimeError("VIDEO_FASTSTART_TIMEOUT 必须大于 0")
        if self.media_cache_slice_mib not in {1, 2, 4} or self.media_cache_ttl_seconds <= 0:
            raise RuntimeError("媒体缓存切片大小必须为 1、2 或 4 MiB，保留时间必须大于 0")
        if self.media_cache_max_mib < 0 or self.media_cache_min_free_mib < 0:
            raise RuntimeError("媒体缓存容量和磁盘保留空间不能为负数")
        if not 1 <= self.cdn_warning_percent < self.cdn_stop_percent <= 100:
            raise RuntimeError("CDN 阈值必须满足 1 <= 预警比例 < 停止比例 <= 100")
        if self.channel_sync_interval < 5:
            raise RuntimeError("CHANNEL_SYNC_INTERVAL 必须至少为 5 秒")
        if self.tg_mode not in {"user", "bot"}:
            raise RuntimeError("TG_MODE 只能是 user 或 bot")
        if self.upload_concurrency <= 0:
            raise RuntimeError("UPLOAD_CONCURRENCY 必须大于 0")
        if not 1 <= self.tg_upload_connections <= 8:
            raise RuntimeError("TG_UPLOAD_CONNECTIONS 必须在 1 到 8 之间")
        if self.upload_chunk_size <= 0:
            raise RuntimeError("UPLOAD_CHUNK_SIZE 必须大于 0")
        if self.tg_segment_size <= 0:
            raise RuntimeError("TG_SEGMENT_SIZE 必须大于 0")
        if self.max_file_size < 0:
            raise RuntimeError("MAX_FILE_SIZE 不能为负数")
        if self.login_max_attempts <= 0 or self.login_window_seconds <= 0:
            raise RuntimeError("登录限流配置必须大于 0")
        if self.upload_stale_hours < 0:
            raise RuntimeError("UPLOAD_STALE_HOURS 不能为负数")
        if self.upload_temp_max_mib < 0 or self.upload_temp_min_free_mib < 0:
            raise RuntimeError("上传临时目录额度和磁盘保留空间不能为负数")
        if self.is_production:
            if len(self.app_secret) < 32 or self.app_secret == "change-this-secret-in-env":
                raise RuntimeError("生产环境必须配置至少 32 位的 APP_SECRET")
            if self.app_password == "change-me":
                raise RuntimeError("生产环境不能使用默认 APP_PASSWORD")


settings = Settings()
