from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.api.routes import (
    accounts,
    auth,
    clients,
    files,
    images,
    items,
    picgo,
    system,
    uploads,
    video_import,
    webdav,
)
from app.config import ROOT_DIR, Settings
from app.config import settings as default_settings
from app.core.errors import install_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.core.security import LoginRateLimiter
from app.infrastructure.db import Database
from app.infrastructure.storage import TelegramStorage
from app.services.accounts import AccountManager, Runtime, WorkspaceMiddleware, account_settings
from app.services.cdn import CDNMonitor
from app.services.channel_sync import ChannelSyncService
from app.services.media_cache import MIB, MediaSliceCache
from app.services.thumbnails import ThumbnailCache
from app.services.traffic import TrafficMiddleware, TrafficMonitor
from app.services.upload_temp import cleanup_faststart_outputs
from app.services.uploads import UploadService, UploadSpace
from app.services.video_import import VideoImport

logger = logging.getLogger("tgdrive")
APP_VERSION = (ROOT_DIR / "VERSION").read_text(encoding="utf-8").strip()


def create_app(
    app_settings: Settings | None = None,
    db: Database | None = None,
    storage: TelegramStorage | None = None,
) -> FastAPI:
    cfg = app_settings or default_settings
    cfg.validate()
    cfg.ensure_dirs()
    configure_logging(cfg.log_level, cfg.log_json)

    database = db or Database(cfg.db_path)
    control = database
    profile = control.active_telegram_profile() if storage is None else None
    if profile:
        key = control.telegram_workspace(profile["chat_id"])
        if key is None:
            raise RuntimeError("当前 Telegram 频道缺少数据库映射，请恢复完整的数据备份")
        if key:
            if not re.fullmatch(r"[a-f0-9]{32}", key):
                raise RuntimeError("频道数据库配置无效")
            database = Database(cfg.data_dir / "channels" / f"{key}.sqlite3")
    telegram = storage or TelegramStorage(account_settings(cfg, profile))
    traffic = TrafficMonitor(control, enabled=cfg.traffic_enabled)
    cdn = CDNMonitor(control, cfg)
    traffic.cdn = cdn
    telegram.traffic = traffic if cfg.traffic_enabled else None
    upload_service = UploadService(
        database,
        telegram,
        cfg.upload_stale_hours,
        cfg.upload_concurrency,
        space=UploadSpace(cfg.upload_temp_max_mib, cfg.upload_temp_min_free_mib),
        faststart=cfg.video_faststart,
        faststart_timeout=cfg.video_faststart_timeout,
        image_host_enabled=cfg.image_host_enabled,
    )

    channel_sync = ChannelSyncService(database, telegram, cfg)

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        cleanup_faststart_outputs(cfg.temp_dir)
        upload_service.cleanup_receiving(all_pending=True)
        interrupted = upload_service.recover_interrupted()
        if interrupted:
            logger.warning("Marked %s interrupted upload(s) as retryable errors", interrupted)
        upload_service.cleanup_stale()
        await telegram.start()
        upload_service.start()
        await channel_sync.start()
        await account_manager.start()
        app.state.video_import.resume(account_manager.current.uploads)
        if telegram.error:
            logger.warning("Telegram unavailable: %s", telegram.error)
        logger.info(
            "TG Drive started version=%s env=%s db_schema=%s storage_configured=%s",
            APP_VERSION,
            cfg.app_env,
            database.schema_version(),
            cfg.is_configured,
        )
        traffic_worker = (
            asyncio.create_task(traffic.persist_periodically(), name="traffic-persistence")
            if cfg.traffic_enabled
            else None
        )
        try:
            app.state.media_cache.start()
            await cdn.start()
            yield
        finally:
            await app.state.media_cache.close()
            await cdn.stop()
            if traffic_worker is not None:
                traffic_worker.cancel()
                await asyncio.gather(traffic_worker, return_exceptions=True)
            await app.state.video_import.stop()
            await account_manager.shutdown()
            logger.info("TG Drive stopped")

    app = FastAPI(
        title=cfg.app_name,
        version=APP_VERSION,
        docs_url=None if cfg.is_production else "/api/docs",
        redoc_url=None,
        openapi_url=None if cfg.is_production else "/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = cfg
    app.state.db = database
    app.state.storage = telegram
    app.state.traffic = traffic
    app.state.cdn = cdn
    app.state.thumbnails = ThumbnailCache(upload_service.video_slots)
    app.state.media_cache = MediaSliceCache(
        cfg.media_cache_dir or cfg.data_dir / "media-slices",
        cfg.media_cache_max_mib * MIB,
        cfg.media_cache_min_free_mib * MIB,
        slice_bytes=cfg.media_cache_slice_mib * MIB,
        ttl_seconds=cfg.media_cache_ttl_seconds,
    )
    app.state.uploads = upload_service
    app.state.channel_sync = channel_sync
    app.state.login_limiter = LoginRateLimiter(cfg.login_max_attempts, cfg.login_window_seconds)
    app.state.picgo_slots = asyncio.Semaphore(2)
    app.state.index_backup_lock = asyncio.Lock()
    account_manager = AccountManager(
        app, cfg, control, Runtime(database, telegram, upload_service, channel_sync)
    )
    app.state.accounts = account_manager
    app.state.video_import = VideoImport(control, cfg)

    app.add_middleware(WorkspaceMiddleware, manager=account_manager)
    app.add_middleware(
        SessionMiddleware,
        secret_key=cfg.app_secret,
        same_site="lax",
        https_only=cfg.cookie_secure,
        max_age=cfg.session_max_age,
    )
    app.add_middleware(RequestContextMiddleware)
    if cfg.traffic_enabled:
        app.add_middleware(TrafficMiddleware, monitor=traffic)
    install_exception_handlers(app)

    app.include_router(system.router)
    app.include_router(accounts.router)
    app.include_router(clients.router)
    app.include_router(webdav.router)
    app.include_router(auth.router)
    app.include_router(items.router)
    app.include_router(uploads.router)
    app.include_router(video_import.router)
    if cfg.image_host_enabled:
        app.include_router(picgo.router)
        app.include_router(images.router)
        app.include_router(images.public_router)
    app.include_router(files.auth_router)
    app.include_router(files.router)

    if cfg.frontend_dist.is_dir():
        assets_dir = cfg.frontend_dist / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    @app.get("/", include_in_schema=False)
    @app.get("/login", include_in_schema=False)
    @app.get("/drive", include_in_schema=False)
    @app.get("/people", include_in_schema=False)
    @app.get("/images", include_in_schema=False)
    @app.get("/traffic", include_in_schema=False)
    @app.get("/sync", include_in_schema=False)
    @app.get("/video-import", include_in_schema=False)
    @app.get("/clients", include_in_schema=False)
    async def index() -> Response:
        if cfg.index_file.is_file():
            return FileResponse(cfg.index_file, headers={"Cache-Control": "no-cache"})
        return HTMLResponse(
            """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>TG Drive - frontend not built</title>
<body style="font-family:system-ui;background:#08111f;color:#e6eefb;padding:40px">
<h1>前端尚未构建</h1>
<p>请运行 <code>cd frontend &amp;&amp; npm ci &amp;&amp; npm run build</code>，
或使用 Docker 镜像（构建阶段会自动编译前端）。</p>
</body></html>""",
            status_code=503,
        )

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    return app


app = create_app()
