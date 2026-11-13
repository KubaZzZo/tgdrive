from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO
from weakref import WeakValueDictionary

from anyio import CancelScope

from app.db import Database
from app.services.image_host import MAX_IMAGE_SIZE, image_mime
from app.storage import TelegramStorage

logger = logging.getLogger("tgdrive.uploads")


class UploadSpaceError(Exception):
    pass


class UploadSpace:
    def __init__(self, max_mib: int = 51200, min_free_mib: int = 512):
        self.max_bytes = max_mib * 1024**2
        self.min_free_bytes = min_free_mib * 1024**2
        self._lock = asyncio.Lock()

    def _check(self, directory: Path, growth: int) -> None:
        if self.max_bytes:
            used = 0
            for path in directory.glob("upload_*.part"):
                try:
                    used += path.stat().st_size
                except FileNotFoundError:
                    # A finished or canceled upload may have released space.
                    continue
            if used + growth > self.max_bytes:
                raise UploadSpaceError("上传临时目录额度不足，请等待现有任务完成或取消后重试")
        if shutil.disk_usage(directory).free < self.min_free_bytes + growth:
            raise UploadSpaceError("上传磁盘可用空间不足，请释放空间后重试")

    async def check_admission(self, directory: Path, size: int) -> None:
        async with self._lock:
            await asyncio.to_thread(self._check, directory, size)

    def _write(self, directory: Path, file: BinaryIO, data: bytes) -> None:
        self._check(directory, len(data))
        if file.write(data) != len(data):
            raise UploadSpaceError("上传磁盘空间不足，请释放空间后重试")

    async def write(self, directory: Path, file: BinaryIO, data: bytes) -> None:
        async with self._lock:
            write = asyncio.get_running_loop().run_in_executor(
                None, self._write, directory, file, data
            )
            try:
                await asyncio.shield(write)
            except asyncio.CancelledError:
                # Let the disk write settle before the caller rolls back and closes the file.
                with CancelScope(shield=True):
                    while not write.done():
                        try:
                            await asyncio.shield(write)
                        except asyncio.CancelledError:
                            continue
                        except Exception:
                            break
                    if not write.cancelled():
                        write.exception()
                raise


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class UploadService:
    """Run browser-upload finalization outside the request lifecycle."""

    def __init__(
        self,
        db: Database,
        storage: TelegramStorage,
        stale_hours: int = 72,
        concurrency: int = 2,
        space: UploadSpace | None = None,
        locks: WeakValueDictionary[str, asyncio.Lock] | None = None,
    ):
        self.db = db
        self.storage = storage
        self.stale_hours = stale_hours
        self.space = space if space is not None else UploadSpace()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._closed = False
        self.locks = locks if locks is not None else WeakValueDictionary[str, asyncio.Lock]()
        self._slots = asyncio.Semaphore(concurrency)
        self._maintenance: asyncio.Task[None] | None = None

    @asynccontextmanager
    async def lock(self, upload_id: str) -> AsyncIterator[None]:
        lock = self.locks.setdefault(upload_id, asyncio.Lock())
        async with lock:
            yield

    def start(self) -> None:
        if self.stale_hours > 0 and self._maintenance is None:
            self._maintenance = asyncio.create_task(self._maintain(), name="upload-cleanup")

    async def _maintain(self) -> None:
        while True:
            await asyncio.sleep(300)
            try:
                await self.cleanup_stale_async()
            except Exception:
                logger.exception("Upload cleanup failed")

    async def cleanup_stale_async(self) -> None:
        cutoff = (datetime.now(UTC) - timedelta(hours=self.stale_hours)).isoformat()
        for upload in self.db.list_stale_uploads(cutoff):
            async with self.lock(upload["id"]):
                current = self.db.get_upload(upload["id"])
                if (
                    current
                    and current["state"] in {"uploading", "error"}
                    and current["updated_at"] < cutoff
                ):
                    Path(current["temp_path"]).unlink(missing_ok=True)
                    self.db.delete_upload(current["id"])

    async def _run(self, upload_id: str) -> None:
        try:
            async with self._slots:
                await self._process(upload_id)
        except asyncio.CancelledError:
            self.db.update_upload(upload_id, state="error", error="服务关闭，上传已中断，请重试")
            raise

    def schedule(self, upload_id: str) -> bool:
        if self._closed or upload_id in self._tasks:
            return False
        task = asyncio.create_task(self._run(upload_id), name=f"upload:{upload_id}")
        self._tasks[upload_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(upload_id, None))
        return True

    async def _process(self, upload_id: str) -> None:
        upload = self.db.get_upload(upload_id)
        if not upload or upload["state"] == "done":
            return
        path = Path(upload["temp_path"])
        if not path.exists():
            self.db.update_upload(upload_id, state="error", error="临时文件不存在，请重新上传")
            return

        self.db.update_upload(upload_id, state="processing", progress=0, error=None)
        last_update = 0.0

        async def progress(sent: int, total: int) -> None:
            nonlocal last_update
            now = time.monotonic()
            if now - last_update >= 0.8 or sent >= total:
                last_update = now
                self.db.update_upload(upload_id, progress=sent)

        parts: list[dict[str, Any]] = []
        committed = False
        try:
            if path.stat().st_size != int(upload["size"]):
                raise ValueError("临时文件大小不匹配，请取消后重新上传")
            if upload["image_host"]:
                if int(upload["size"]) > MAX_IMAGE_SIZE:
                    raise ValueError("图床单张图片不能超过 20 MiB")
                with path.open("rb") as source:
                    upload["mime"] = image_mime(source.read(32))
            parent = self.db.get_item(upload["parent_id"]) if upload["parent_id"] else None
            if upload["parent_id"] and (not parent or parent["kind"] != "folder"):
                raise ValueError("目标文件夹已删除，请取消后重新选择上传目录")
            sha256 = await asyncio.to_thread(file_sha256, path)
            parts = await self.storage.upload_file(
                path=path,
                name=upload["name"],
                mime=upload["mime"],
                progress=progress,
                storage_tag=upload_id,
            )
            item = self.db.create_file(
                name=upload["name"],
                parent_id=upload["parent_id"],
                size=int(upload["size"]),
                mime=upload["mime"],
                sha256=sha256,
                chat_id=str(self.storage.chat),
                parts=parts,
                hosted_mime=upload["mime"] if upload["image_host"] else None,
                upload_id=upload_id,
            )
            committed = True
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove temp upload %s", path)
            logger.info(
                "Upload completed: %s (%s bytes, %s parts)",
                item["name"],
                item["size"],
                len(parts),
            )
        except asyncio.CancelledError:
            self.db.update_upload(
                upload_id, state="error", error="服务正在关闭，Telegram 写入已中断，请重试"
            )
            raise
        except Exception as exc:
            logger.exception("Upload failed: %s", upload_id)
            self.db.update_upload(upload_id, state="error", error=str(exc))
        finally:
            if parts and not committed:
                try:
                    await self.storage.delete_messages(
                        [int(part["tg_message_id"]) for part in parts]
                    )
                except Exception:
                    logger.exception("Failed to remove uncommitted Telegram segments")

    def cleanup_stale(self) -> int:
        """Remove unfinished upload records and their temp files after a TTL."""
        if self.stale_hours <= 0:
            return 0
        cutoff = (datetime.now(UTC) - timedelta(hours=self.stale_hours)).isoformat()
        removed = 0
        for upload in self.db.list_stale_uploads(cutoff):
            Path(upload["temp_path"]).unlink(missing_ok=True)
            self.db.delete_upload(upload["id"])
            removed += 1
        if removed:
            logger.info("Removed %s stale upload(s)", removed)
        return removed

    async def shutdown(self) -> None:
        self._closed = True
        tasks = list(self._tasks.values())
        if self._maintenance:
            tasks.append(self._maintenance)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def active_count(self) -> int:
        return len(self._tasks)

    def status(self) -> dict[str, Any]:
        return {"active": len(self._tasks), "closed": self._closed}
