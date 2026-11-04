from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.api.routes import auth, files, items, system, uploads
from app.config import Settings
from app.config import settings as default_settings
from app.core.errors import install_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.core.security import LoginRateLimiter
from app.db import Database
from app.services.channel_sync import ChannelSyncService
from app.services.uploads import UploadService
from app.storage import TelegramStorage

logger = logging.getLogger("tgdrive")


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
    telegram = storage or TelegramStorage(cfg)
    upload_service = UploadService(
        database, telegram, cfg.upload_stale_hours, cfg.upload_concurrency
    )

    channel_sync = ChannelSyncService(database, telegram, cfg)

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        interrupted = database.recover_interrupted_uploads()
        if interrupted:
            logger.warning("Marked %s interrupted upload(s) as retryable errors", interrupted)
        upload_service.cleanup_stale()
        await telegram.start()
        upload_service.start()
        await channel_sync.start()
        if telegram.error:
            logger.warning("Telegram unavailable: %s", telegram.error)
        logger.info(
            "TG Drive started env=%s db_schema=%s storage_configured=%s",
            cfg.app_env,
            database.schema_version(),
            cfg.is_configured,
        )
        try:
            yield
        finally:
            await channel_sync.shutdown()
            await upload_service.shutdown()
            await telegram.stop()
            database.close()
            logger.info("TG Drive stopped")

    app = FastAPI(
        title=cfg.app_name,
        version="2.0.0",
        docs_url=None if cfg.is_production else "/api/docs",
        redoc_url=None,
        openapi_url=None if cfg.is_production else "/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = cfg
    app.state.db = database
    app.state.storage = telegram
    app.state.uploads = upload_service
    app.state.channel_sync = channel_sync
    app.state.login_limiter = LoginRateLimiter(cfg.login_max_attempts, cfg.login_window_seconds)

    app.add_middleware(
        SessionMiddleware,
        secret_key=cfg.app_secret,
        same_site="lax",
        https_only=cfg.cookie_secure,
        max_age=cfg.session_max_age,
    )
    app.add_middleware(RequestContextMiddleware)
    install_exception_handlers(app)

    app.include_router(system.router)
    app.include_router(auth.router)
    app.include_router(items.router)
    app.include_router(uploads.router)
    app.include_router(files.auth_router)
    app.include_router(files.router)

    if cfg.frontend_dist.is_dir():
        assets_dir = cfg.frontend_dist / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    @app.get("/", include_in_schema=False)
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
