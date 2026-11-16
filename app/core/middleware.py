from __future__ import annotations

import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import request_id_ctx, request_log_path

logger = logging.getLogger("tgdrive.http")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach a request id and emit one structured access log per API request."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("X-Request-ID", "").strip()[:64] or uuid.uuid4().hex
        token = request_id_ctx.set(request_id)
        request.state.request_id = request_id
        started = time.perf_counter()
        status_code = 500
        path = request.url.path
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("Referrer-Policy", "no-referrer")
            response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
            if request.url.path.startswith("/api/"):
                response.headers.setdefault("Cache-Control", "no-store")
            if path == "/webdav" or path.startswith(("/i/", "/s/", "/webdav/")):
                response.headers["Cache-Control"] = "private, no-store"
                response.headers["Cloudflare-CDN-Cache-Control"] = "no-store"
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
                "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; "
                "frame-ancestors 'self'",
            )
            return response
        finally:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            if not path.startswith(("/static/", "/assets/")):
                path = request_log_path(path)
                logger.info(
                    "%s %s %s %.1fms",
                    request.method,
                    path,
                    status_code,
                    elapsed_ms,
                    extra={
                        "method": request.method,
                        "path": path,
                        "status": status_code,
                        "duration_ms": elapsed_ms,
                        "client_ip": request.client.host if request.client else None,
                    },
                )
            request_id_ctx.reset(token)
