from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from pathlib import Path
from typing import Any

from app.config import Settings
from app.infrastructure.db import Database
from app.services.ed2k_source import download_ed2k, parse_link, work_dir
from app.services.upload_temp import remove_upload_files
from app.services.uploads import UploadService, UploadSpaceError
from app.services.video_source import open_video, validate_url, video_metadata

logger = logging.getLogger("tgdrive.video_import")
TERMINAL = {"done", "cancelled"}


class VideoImport:
    """One durable batch, shared across channel workspaces."""

    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.directory = (
            settings.video_import_dir or settings.data_dir / "video-imports"
        ).resolve()
        limits = [settings.video_import_max_mib * 1024**2]
        if settings.max_file_size:
            limits.append(settings.max_file_size)
        self.max_bytes = min(limits)
        self.ed2k_seeds = settings.video_import_ed2k_seeds
        self.chunk_size = settings.upload_chunk_size
        self.batch = db.get_video_import()
        self.worker: asyncio.Task[None] | None = None

    @property
    def active(self) -> bool:
        return self.batch is not None and not all(
            job["state"] in TERMINAL for job in self.batch["jobs"]
        )

    @property
    def cancelling(self) -> bool:
        return bool(self.batch and self.batch["cancel"])

    def _save(self) -> None:
        assert self.batch is not None
        self.db.save_video_import(self.batch)

    def status(self, uploads: UploadService) -> dict:
        jobs = []
        for job in self.batch["jobs"] if self.batch else []:
            info = {key: value for key, value in job.items() if key != "url"}
            if self.batch["workspace"] == uploads.db.path and job["state"] == "uploading":
                upload = uploads.db.get_upload(job["id"])
                if upload:
                    info.update(uploaded=upload["progress"], size=upload["size"])
            jobs.append(info)
        return {
            "active": self.active,
            "running": bool(self.worker and not self.worker.done()),
            "jobs": jobs,
        }

    def start(self, uploads: UploadService, links: list[str]) -> None:
        if self.active or (self.worker and not self.worker.done()):
            raise RuntimeError("上一批视频的临时文件尚未清理")
        if not 1 <= len(links) <= 2:
            raise ValueError("每批最多导入两个视频")
        jobs = []
        for link in links:
            job = {"id": uuid.uuid4().hex, "url": link, "state": "queued", "downloaded": 0}
            if link.lower().startswith("ed2k:"):
                name, size, _, normalized = parse_link(link)
                if self.ed2k_seeds is None:
                    raise ValueError("服务器尚未启用 eD2k 导入")
                if size > self.max_bytes:
                    raise ValueError("视频超过导入大小上限")
                job.update(name=name, size=size, url=normalized)
            else:
                job["url"] = str(validate_url(link))
            jobs.append(job)
        previous = self.batch
        batch_id = uuid.uuid4().hex
        self.batch = {
            "directory": str(self.directory / batch_id),
            "workspace": uploads.db.path,
            "cancel": False,
            "jobs": jobs,
        }
        try:
            self._save()
        except Exception:
            self.batch = previous
            raise
        self.resume(uploads)

    def resume(self, uploads: UploadService, *, retry: bool = False) -> None:
        if not self.active or not self.batch or self.batch["workspace"] != uploads.db.path:
            return
        if self.worker and not self.worker.done():
            return
        self.worker = asyncio.create_task(self._run(uploads, retry), name="video-import")

    async def stop(self) -> None:
        if self.worker and not self.worker.done():
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)

    def cancel(self, uploads: UploadService) -> None:
        if not self.active or not self.batch:
            return
        if not self.cancelling:
            self.batch["cancel"] = True
            try:
                self._save()
            except Exception:
                self.batch["cancel"] = False
                raise
        # Let a Telegram write finish. Cancelling it mid-flight risks orphaned messages.
        self.resume(uploads)

    async def _run(self, uploads: UploadService, retry: bool) -> None:
        assert self.batch is not None
        directory = Path(self.batch["directory"])
        await asyncio.gather(
            *(
                self._process(uploads, directory, job, index, retry)
                for index, job in enumerate(self.batch["jobs"], 1)
                if job["state"] not in TERMINAL
            )
        )
        if (
            self.batch["cancel"]
            and self.active
            and any(job["state"] == "error" for job in self.batch["jobs"])
        ):
            await asyncio.gather(
                *(
                    self._process(uploads, directory, job, index, True)
                    for index, job in enumerate(self.batch["jobs"], 1)
                    if job["state"] == "error"
                )
            )

    async def _process(
        self, uploads: UploadService, directory: Path, job: dict, index: int, retry: bool
    ) -> None:
        assert self.batch is not None
        path = directory / f"upload_{job['id']}.part"
        try:
            upload = uploads.db.get_upload(job["id"])
            if self.batch["cancel"] or (upload and upload["state"] == "done"):
                self._clean(
                    directory,
                    path,
                    job,
                    "done" if upload and upload["state"] == "done" else "cancelled",
                )
                if upload and upload["state"] != "done":
                    uploads.db.delete_upload(job["id"])
                return
            if job["state"] == "error" and not retry:
                return
            if not upload:
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                job.update(state="downloading", error=None, downloaded=0)
                job.pop("ed2k_sources", None)
                self._save()
                async with asyncio.timeout(3600):
                    name, mime, size = await self._download(uploads, path, job, index)
                if self.batch["cancel"]:
                    self._clean(directory, path, job, "cancelled")
                    return
                job.update(name=name, size=size)
                uploads.db.create_upload(
                    None, name, size, mime, str(path), self.chunk_size, import_id=job["id"]
                )
            job.update(state="uploading", error=None)
            self._save()
            await uploads.run_import(job["id"])
            result = uploads.db.get_upload(job["id"])
            if not result or result["state"] != "done":
                raise ValueError("上传 Telegram 失败，可重试或取消")
            self._clean(directory, path, job, "done")
        except Exception as exc:
            # External URLs may include signed credentials. Never return or log HTTP errors verbatim.
            logger.warning("Video import %s failed (%s)", job["id"], type(exc).__name__)
            job.update(
                state="error",
                error=(
                    str(exc)
                    if type(exc) in {ValueError, UploadSpaceError}
                    else "下载或清理失败，可重试或取消"
                ),
            )
            self._save()

    def _clean(self, directory: Path, path: Path, job: dict, state: str) -> None:
        if work_dir(path).exists():
            shutil.rmtree(work_dir(path))
        remove_upload_files(path)
        assert self.batch is not None
        if directory.exists() and not any(
            other is not job and other["state"] not in TERMINAL for other in self.batch["jobs"]
        ):
            directory.rmdir()
        previous = job.copy()
        job.update(state=state, error=None)
        job.pop("url", None)
        try:
            self._save()
        except Exception:
            job.clear()
            job.update(previous)
            raise

    async def _download(
        self, uploads: UploadService, path: Path, job: dict[str, Any], index: int
    ) -> tuple[str, str, int]:
        if job["url"].startswith("ed2k:"):
            if self.ed2k_seeds is None:
                raise ValueError("服务器尚未启用 eD2k 导入")
            if "size" not in job:
                job["size"] = parse_link(job["url"])[1]

            def report_progress(downloaded: int, sources: int | None) -> None:
                job["downloaded"] = downloaded
                if sources is None:
                    job.pop("ed2k_sources", None)
                else:
                    job["ed2k_sources"] = sources

            return await download_ed2k(
                job["url"],
                path,
                self.ed2k_seeds,
                uploads.space,
                self.max_bytes,
                lambda: self.cancelling,
                report_progress,
            )
        job.pop("size", None)
        url = validate_url(job["url"])
        async with open_video(url) as (response, final_url):
            name, mime = video_metadata(final_url, response, index)
            length = response.headers.get("content-length")
            expected = int(length) if length and length.isascii() and length.isdigit() else None
            if expected is not None and not 0 < expected <= self.max_bytes:
                raise ValueError("视频超过导入大小上限或文件为空")
            if expected is not None:
                job["size"] = expected
            with path.open("wb", buffering=0) as file:
                if expected:
                    await uploads.space.check_admission(path.parent, expected)
                size = 0
                async for chunk in response.aiter_raw(1024**2):
                    if self.batch and self.batch["cancel"]:
                        break
                    size += len(chunk)
                    if size > self.max_bytes or (expected is not None and size > expected):
                        raise ValueError("视频超过导入大小上限")
                    await uploads.space.write(path.parent, file, chunk)
                    job["downloaded"] = size
                if (
                    self.batch
                    and not self.batch["cancel"]
                    and (not size or (expected is not None and size != expected))
                ):
                    raise ValueError("视频为空或下载不完整")
            return name, mime, size
