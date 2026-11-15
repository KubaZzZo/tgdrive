import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field

from app.core.security import require_auth
from app.services.torrent_source import TORRENT_ID


class VideoImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    links: list[str] = Field(min_length=1, max_length=2)


class VideoImportPreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    links: list[str] = Field(min_length=1, max_length=100)


router = APIRouter(
    prefix="/api/video-import", tags=["video import"], dependencies=[Depends(require_auth)]
)


@router.get("")
async def status(request: Request):
    return request.app.state.video_import.status(request.app.state.accounts.current.uploads)


@router.get("/downloads")
async def downloads(request: Request):
    try:
        return await request.app.state.video_import.inbox()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, "暂时无法读取 qBittorrent 下载列表") from None


@router.post("/downloads/{digest}/import", status_code=202)
async def import_download(request: Request, digest: str = Path(pattern=TORRENT_ID.pattern + "$")):
    manager = request.app.state.accounts
    service = request.app.state.video_import
    async with manager.lock:
        runtime = manager.current
        if not runtime.storage.status()["connected"]:
            raise HTTPException(503, "Telegram 尚未连接")
        try:
            await service.start_inbox(runtime.uploads, digest)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from None
        except httpx.HTTPError:
            raise HTTPException(502, "暂时无法连接 qBittorrent") from None
        return service.status(runtime.uploads)


@router.delete("/downloads/{digest}", status_code=204)
async def delete_download(request: Request, digest: str = Path(pattern=TORRENT_ID.pattern + "$")):
    async with request.app.state.accounts.lock:
        try:
            await request.app.state.video_import.delete_inbox(digest)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from None
        except httpx.HTTPError:
            raise HTTPException(502, "暂时无法连接 qBittorrent") from None


@router.post("/preview")
async def preview(body: VideoImportPreviewBody, request: Request):
    return {"rows": request.app.state.video_import.preview(body.links)}


@router.post("", status_code=202)
async def start(body: VideoImportBody, request: Request):
    manager = request.app.state.accounts
    service = request.app.state.video_import
    async with manager.lock:
        runtime = manager.current
        if not runtime.storage.status()["connected"]:
            raise HTTPException(503, "Telegram 尚未连接")
        try:
            service.start(runtime.uploads, body.links)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from None
        return service.status(runtime.uploads)


@router.post("/retry", status_code=202)
async def retry(request: Request):
    manager = request.app.state.accounts
    service = request.app.state.video_import
    async with manager.lock:
        runtime = manager.current
        if not runtime.storage.status()["connected"] and not service.cancelling:
            raise HTTPException(503, "Telegram 尚未连接")
        service.resume(runtime.uploads, retry=True)
        return service.status(runtime.uploads)


@router.post("/cancel", status_code=202)
async def cancel(request: Request):
    manager = request.app.state.accounts
    service = request.app.state.video_import
    async with manager.lock:
        service.cancel(manager.current.uploads)
        return service.status(manager.current.uploads)
