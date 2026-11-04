from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger("tgdrive.error")


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "-")


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail
        if not isinstance(detail, (str, dict, list)):
            detail = str(detail)
        headers = dict(exc.headers or {})
        headers.setdefault("X-Request-ID", _request_id(request))
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": detail, "request_id": _request_id(request)},
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "detail": "请求参数校验失败",
                "errors": jsonable_encoder(
                    [
                        {key: value for key, value in error.items() if key not in {"input", "ctx"}}
                        for error in exc.errors()
                    ]
                ),
                "request_id": _request_id(request),
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": "服务器内部错误", "request_id": _request_id(request)},
        )
