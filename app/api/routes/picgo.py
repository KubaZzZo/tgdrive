from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from python_multipart import MultipartParser
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import parse_options_header

from app.api.client_auth import require_picgo
from app.api.deps import get_db, get_settings, get_upload_service, require_storage
from app.api.request_body import bounded_body
from app.api.routes.uploads import create_upload_record, is_disk_full
from app.api.schemas.uploads import UploadCreateBody
from app.config import Settings
from app.infrastructure.db import Database
from app.infrastructure.storage import TelegramStorage
from app.services.image_host import MAX_IMAGE_SIZE, image_mime
from app.services.uploads import UploadService, UploadSpaceError

router = APIRouter(prefix="/api/picgo", tags=["picgo"], dependencies=[Depends(require_picgo)])
MULTIPART_OVERHEAD = 64 * 1024


async def receive_image(request: Request, limit: int) -> tuple[str, bytearray]:
    media_type, options = parse_options_header(request.headers.get("content-type", ""))
    boundary = options.get(b"boundary", b"")
    if media_type != b"multipart/form-data" or not 0 < len(boundary) <= 70:
        raise HTTPException(415, "请使用 multipart/form-data 上传图片")

    name = ""
    data = bytearray()
    headers: dict[bytes, bytes] = {}
    field, value = bytearray(), bytearray()
    header_bytes = 0
    parts = 0
    finished = False

    def part_begin() -> None:
        nonlocal parts
        parts += 1
        if parts != 1:
            raise HTTPException(400, "一次只接受一个 file 字段")

    def header_fragment(target: bytearray, buffer: bytes, start: int, end: int) -> None:
        nonlocal header_bytes
        header_bytes += end - start
        if header_bytes > MULTIPART_OVERHEAD:
            raise HTTPException(413, "图片表单头过大")
        target.extend(buffer[start:end])

    def header_end() -> None:
        headers[bytes(field).lower()] = bytes(value)
        field.clear()
        value.clear()

    def headers_finished() -> None:
        nonlocal name
        disposition, parameters = parse_options_header(headers.get(b"content-disposition", b""))
        if disposition != b"form-data" or parameters.get(b"name") != b"file":
            raise HTTPException(400, "缺少 file 图片字段")
        filename = parameters.get(b"filename", b"")
        try:
            name = filename.decode("utf-8")
        except UnicodeDecodeError:
            name = filename.decode("latin-1")
        if not name.strip() or len(name) > 255:
            raise HTTPException(400, "图片文件名无效")

    def part_data(buffer: bytes, start: int, end: int) -> None:
        if len(data) + end - start > limit:
            raise HTTPException(413, "图片超过大小限制")
        data.extend(memoryview(buffer)[start:end])

    def finish() -> None:
        nonlocal finished
        finished = True

    parser = MultipartParser(
        boundary,
        {
            "on_part_begin": part_begin,
            "on_header_field": lambda *args: header_fragment(field, *args),
            "on_header_value": lambda *args: header_fragment(value, *args),
            "on_header_end": header_end,
            "on_headers_finished": headers_finished,
            "on_part_data": part_data,
            "on_end": finish,
        },
    )
    try:
        async for chunk in bounded_body(request, limit + MULTIPART_OVERHEAD):
            parser.write(chunk)
        parser.finalize()
    except MultipartParseError:
        raise HTTPException(400, "图片表单无效") from None
    if not finished or not name or not data:
        raise HTTPException(400, "图片上传不完整")
    return name, data


@router.post("/upload")
async def upload_image(
    request: Request,
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_settings),
    storage: TelegramStorage = Depends(require_storage),
    uploads: UploadService = Depends(get_upload_service),
) -> dict:
    if not uploads.image_host_enabled:
        raise HTTPException(404, "图床功能已关闭")
    limit = min(MAX_IMAGE_SIZE, settings.max_file_size or MAX_IMAGE_SIZE)
    slots: asyncio.Semaphore = request.app.state.picgo_slots
    if slots.locked():
        raise HTTPException(429, "图片上传繁忙，请稍后重试", headers={"Retry-After": "5"})
    async with slots:
        name, data = await receive_image(request, limit)
        try:
            mime = image_mime(bytes(data[:32]))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        body = UploadCreateBody(name=name, size=len(data), mime=mime, image_host=True)
        upload = await create_upload_record(body, db, settings, uploads)
        upload_id = upload["id"]
        async with uploads.lock(upload_id):
            path = Path(upload["temp_path"])
            try:
                with path.open("wb", buffering=0) as output:
                    await uploads.space.write(path.parent, output, data)
                del data
                db.update_upload(upload_id, received=body.size)
                task = uploads.schedule(upload_id, discard_on_error=True)
                if task is None:
                    raise HTTPException(409, "账号正在切换，请重试")
            except BaseException as exc:
                uploads.discard_receiving(upload)
                if isinstance(exc, UploadSpaceError):
                    raise HTTPException(507, str(exc)) from None
                if isinstance(exc, OSError) and is_disk_full(exc):
                    raise HTTPException(507, "上传磁盘空间不足，请重试") from None
                raise
        await asyncio.shield(task)
        record = db.get_upload(upload_id)
        image = (
            db.hosted_image_for_item(record["item_id"]) if record and record["item_id"] else None
        )
        if record is None or record["state"] != "done" or image is None:
            raise HTTPException(502, "图片写入 Telegram 失败，请重新上传")
        link = str(request.url_for("public_image", token=image["token"]))
        return {"success": True, "data": {"downloadLink": link}}
