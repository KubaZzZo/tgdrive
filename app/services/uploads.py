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

from app.infrastructure.db import Database
from app.infrastructure.storage import TelegramStorage
from app.services.image_host import MAX_IMAGE_SIZE, image_mime
from app.services.progress_events import ProgressSignal
from app.services.upload_temp import faststart_path, remove_upload_files
from app.services.video_prepare import prepared_video

logger = logging.getLogger("tgdrive.uploads")
RECEIVING_RETENTION_HOURS = 72


class UploadSpaceError(Exception):
    pass


class UploadSpace:
    def __init__(self, max_mib: int = 51200, min_free_mib: int = 512):
        self.max_bytes = max_mib * 1024**2
        self.min_free_bytes = min_free_mib * 1024**2
        self._lock = asyncio.Lock()
        self._reservations: dict[Path, int] = {}

    def _check(self, directory: Path, growth: int) -> None:
        reserved = {
            path: size
            for path, size in self._reservations.copy().items()
            if path.parent == directory
        }
        remaining = 0
        used = 0
        for path, size in reserved.items():
            try:
                actual = path.stat().st_size
            except FileNotFoundError:
                actual = 0
            remaining += max(0, size - actual)
            used += max(size, actual)
        if self.max_bytes:
            for path in directory.glob("upload_*.part"):
                if path in reserved:
                    continue
                try:
                    used += path.stat().st_size
                except FileNotFoundError:
                    # A finished or canceled upload may have released space.
                    continue
            if used + growth > self.max_bytes:
                raise UploadSpaceError("上传临时目录额度不足，请等待现有任务完成或取消后重试")
        if shutil.disk_usage(directory).free < self.min_free_bytes + growth + remaining:
            raise UploadSpaceError("上传磁盘可用空间不足，请释放空间后重试")

    async def check_admission(self, directory: Path, size: int) -> None:
        async with self._lock:
            await asyncio.to_thread(self._check, directory, size)

    @asynccontextmanager
    async def reserve(self, path: Path, size: int, *, existing: int = 0) -> AsyncIterator[None]:
        async with self._lock:
            if path in self._reservations:
                raise UploadSpaceError("上传临时文件正在使用，请重试")
            await asyncio.to_thread(self._check, path.parent, size - existing)
            self._reservations[path] = size
        try:
            yield
        finally:
            # Reservation checks run in a thread. Mutate only on the event loop
            # and leave in-flight checks with a snapshot of the old reservation.
            self._reservations.pop(path)

    @staticmethod
    def _write(file: BinaryIO, data: bytes) -> None:
        if file.write(data) != len(data):
            raise UploadSpaceError("上传磁盘空间不足，请释放空间后重试")

    async def write(self, directory: Path, file: BinaryIO, data: bytes) -> None:
        async with self._lock:
            await asyncio.to_thread(self._check, directory, len(data))
            await self.write_reserved(file, data)

    async def write_reserved(self, file: BinaryIO, data: bytes) -> None:
        write = asyncio.get_running_loop().run_in_executor(None, self._write, file, data)
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
                    error = write.exception()
                    if error is not None:
                        logger.warning(
                            "Disk write failed while canceling upload",
                            exc_info=(type(error), error, error.__traceback__),
                        )
            raise


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


async def upload_sha256(path: Path) -> str:
    # Keep the prepared file alive until the worker has closed its file handle.
    worker = asyncio.create_task(asyncio.to_thread(file_sha256, path))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        with CancelScope(shield=True):
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not worker.cancelled():
                error = worker.exception()
                if error is not None:
                    logger.warning(
                        "File hashing failed while canceling upload",
                        exc_info=(type(error), error, error.__traceback__),
                    )
        raise


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
        faststart: bool = True,
        faststart_timeout: int = 300,
        image_host_enabled: bool = True,
        video_slots: asyncio.Semaphore | None = None,
    ):
        self.db = db
        self.storage = storage
        self.stale_hours = stale_hours
        self.space = space if space is not None else UploadSpace()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stages: dict[str, tuple[str, float]] = {}
        self.progress_events = ProgressSignal()
        self._closed = False
        self.locks = locks if locks is not None else WeakValueDictionary[str, asyncio.Lock]()
        self._slots = asyncio.Semaphore(concurrency)
        self.image_host_enabled = image_host_enabled
        self.video_slots = video_slots if video_slots is not None else asyncio.Semaphore(1)
        self.faststart = faststart
        self.faststart_timeout = faststart_timeout

    @asynccontextmanager
    async def lock(self, upload_id: str) -> AsyncIterator[None]:
        lock = self.locks.setdefault(upload_id, asyncio.Lock())
        async with lock:
            yield

    def discard_upload(self, upload: dict[str, Any]) -> None:
        remove_upload_files(Path(upload["temp_path"]))
        self.db.delete_upload(upload["id"])

    def discard_receiving(self, upload: dict[str, Any]) -> None:
        try:
            self.discard_upload(upload)
        except OSError:
            logger.exception("Could not remove interrupted upload %s", upload["id"])

    def recover_interrupted(self) -> int:
        # An old runtime can still hold a lease during a channel switch.
        for upload in self.db.list_uploads(
            states=("processing", "uploading", "error", "done"), include_imports=True
        ):
            lock = self.locks.get(upload["id"])
            if lock is not None and lock.locked():
                continue
            path = Path(upload["temp_path"])
            try:
                faststart_path(path).unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove leftover faststart output")
            if not upload["link_import"] and upload["state"] in {"uploading", "error"}:
                try:
                    if path.stat().st_size > int(upload["received"]):
                        with path.open("r+b") as file:
                            file.truncate(int(upload["received"]))
                except FileNotFoundError:
                    pass
                except OSError:
                    logger.warning(
                        "Could not truncate unconfirmed upload bytes for %s", upload["id"]
                    )
        return self.db.recover_interrupted_uploads()

    def current_stage(self, upload_id: str) -> str:
        stage = self._stages.get(upload_id)
        return stage[0] if stage else "queued"

    def _set_stage(self, upload_id: str, stage: str | None) -> None:
        now = time.monotonic()
        previous = self._stages.pop(upload_id, None)
        if previous:
            logger.info(
                "Upload %s stage=%s duration_ms=%.0f",
                upload_id,
                previous[0],
                (now - previous[1]) * 1000,
            )
        if stage:
            self._stages[upload_id] = (stage, now)
        self.progress_events.notify()

    async def _run(self, upload_id: str, *, discard_on_error: bool = False) -> None:
        self._set_stage(upload_id, "queued")
        try:
            async with self._slots:
                self._set_stage(upload_id, "preparing")
                await self._process(upload_id)
        except asyncio.CancelledError:
            self.db.update_upload(upload_id, state="error", error="服务关闭，上传已中断，请重试")
            raise
        finally:
            self._set_stage(upload_id, None)
            if discard_on_error:
                upload = self.db.get_upload(upload_id)
                if upload and upload["state"] != "done":
                    self.discard_receiving(upload)
            self.progress_events.notify()

    async def run_import(self, upload_id: str) -> None:
        self.db.update_upload(upload_id, state="processing", error=None, progress=0)
        await self._run(upload_id)

    def schedule(
        self, upload_id: str, *, discard_on_error: bool = False
    ) -> asyncio.Task[None] | None:
        if self._closed or upload_id in self._tasks:
            return None
        task = asyncio.create_task(
            self._run(upload_id, discard_on_error=discard_on_error), name=f"upload:{upload_id}"
        )
        self._tasks[upload_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(upload_id, None))
        return task

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
                value = min(int(upload["size"]), sent * int(upload["size"]) // max(total, 1))
                self.db.update_upload(upload_id, progress=value)
                self.progress_events.notify()

        parts: list[dict[str, Any]] = []
        committed = False
        try:
            # A retry may follow a crash; remove the old output before reserving space.
            faststart_path(path).unlink(missing_ok=True)
            if path.stat().st_size != int(upload["size"]):
                raise ValueError("临时文件大小不匹配，请取消后重新上传")
            if upload["image_host"]:
                if not self.image_host_enabled:
                    raise ValueError("图床功能已关闭")
                if int(upload["size"]) > MAX_IMAGE_SIZE:
                    raise ValueError("图床单张图片不能超过 20 MiB")
                with path.open("rb") as source:
                    upload["mime"] = image_mime(source.read(32))
            parent = self.db.get_item(upload["parent_id"]) if upload["parent_id"] else None
            if upload["parent_id"] and (not parent or parent["kind"] != "folder"):
                raise ValueError("目标文件夹已删除，请取消后重新选择上传目录")
            async with prepared_video(
                path,
                upload["name"],
                self.space,
                enabled=self.faststart and not upload["image_host"],
                timeout=self.faststart_timeout,
                slots=self.video_slots,
            ) as prepared:
                stored_size = prepared.stat().st_size
                self._set_stage(upload_id, "hashing")
                sha256 = await upload_sha256(prepared)
                self._set_stage(upload_id, "sending")
                parts = await self.storage.upload_file(
                    path=prepared,
                    name=upload["name"],
                    mime=upload["mime"],
                    progress=progress,
                    storage_tag=upload_id,
                )
            self._set_stage(upload_id, "committing")
            item = self.db.create_file(
                name=upload["name"],
                parent_id=upload["parent_id"],
                size=stored_size,
                mime=upload["mime"],
                sha256=sha256,
                chat_id=str(self.storage.chat),
                parts=parts,
                hosted_mime=upload["mime"] if upload["image_host"] else None,
                upload_id=upload_id,
            )
            committed = True
            try:
                remove_upload_files(path)
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

    def cleanup_stale(self, db: Database | None = None) -> int:
        """Remove unfinished upload records and their temp files after a TTL."""
        db = self.db if db is None else db
        cutoff = (
            datetime.now(UTC) - timedelta(hours=self.stale_hours or RECEIVING_RETENTION_HOURS)
        ).isoformat()
        stale = (
            db.list_stale_uploads(cutoff) if self.stale_hours else db.list_receiving_uploads(cutoff)
        )
        removed = 0
        for upload in stale:
            lock = self.locks.get(upload["id"])
            if lock is not None and lock.locked():
                continue
            # The event loop cannot start a PATCH or a DELETE during these operations.
            try:
                remove_upload_files(Path(upload["temp_path"]))
                db.delete_upload(upload["id"])
                removed += 1
            except OSError:
                logger.exception("Could not remove stale upload %s", upload["id"])
        if removed:
            logger.info("Removed %s stale upload(s)", removed)
        return removed

    async def shutdown(self) -> None:
        self._closed = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def active_count(self) -> int:
        return len(self._tasks)

    def status(self) -> dict[str, Any]:
        return {"active": len(self._tasks), "closed": self._closed}
