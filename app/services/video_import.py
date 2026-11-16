from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.config import Settings
from app.infrastructure.db import Database
from app.services.ed2k_source import download_ed2k, parse_link, work_dir
from app.services.progress_events import ProgressSignal
from app.services.torrent_source import QBittorrent, parse_magnet
from app.services.upload_temp import remove_upload_files
from app.services.uploads import UploadService, UploadSpaceError
from app.services.video_source import open_video, validate_url, video_metadata

logger = logging.getLogger("tgdrive.video_import")
TERMINAL = {"done", "cancelled"}
PREVIEW_PROTOCOLS = {"http": "http", "https": "http", "ed2k": "ed2k", "magnet": "magnet"}


def _link_key(url: str) -> str:
    if url.startswith("magnet:"):
        return url.split("&", 1)[0]
    return url.split("|")[4] if url.startswith("ed2k:") else url


class VideoImport:
    """One durable batch, shared across channel workspaces."""

    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.directory = (
            settings.video_import_dir or settings.data_dir / "video-imports"
        ).resolve()
        self.inbox_directory = self.directory / "inbox"
        limits = [settings.video_import_max_mib * 1024**2]
        if settings.max_file_size:
            limits.append(settings.max_file_size)
        self.max_bytes = min(limits)
        self.ed2k_seeds = settings.video_import_ed2k_seeds
        self.ed2k_port = settings.video_import_ed2k_port
        self.qb = (
            QBittorrent(
                settings.video_import_qb_url,
                settings.video_import_qb_username,
                settings.video_import_qb_password,
            )
            if settings.video_import_qb_url
            else None
        )
        self.chunk_size = settings.upload_chunk_size
        self.batch = db.get_video_import()
        self.worker: asyncio.Task[None] | None = None
        self.progress_events = ProgressSignal()

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
        self.progress_events.notify()

    def status(self, uploads: UploadService) -> dict:
        batch = self.batch if self.batch and self.batch["workspace"] == uploads.db.path else None
        jobs = []
        for job in batch["jobs"] if batch else []:
            info = {key: value for key, value in job.items() if key != "url"}
            if job["state"] == "uploading":
                upload = uploads.db.get_upload(job["id"])
                if upload:
                    info.update(uploaded=upload["progress"], size=upload["size"])
            jobs.append(info)
        return {
            "active": self.active,
            "running": bool(self.worker and not self.worker.done()),
            "batch_id": Path(batch["directory"]).name if batch else None,
            "jobs": jobs,
        }

    def _prepare_link(self, link: str) -> dict[str, Any]:
        if not link or len(link) > 4096:
            raise ValueError("每条链接须为 1 至 4096 个字符")
        if link.lower().startswith("magnet:"):
            if self.qb is None:
                raise ValueError("服务器尚未启用磁力下载")
            normalized, name = parse_magnet(link)
            return {"url": normalized, "name": name} if name else {"url": normalized}
        if link.lower().startswith("ed2k:"):
            name, size, _, normalized = parse_link(link)
            if self.ed2k_seeds is None:
                raise ValueError("服务器尚未启用 eD2k 导入")
            if size > self.max_bytes:
                raise ValueError("视频超过导入大小上限")
            return {"url": normalized, "name": name, "size": size}
        return {"url": str(validate_url(link))}

    def preview(self, links: list[str]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        rows = []
        for link in links:
            scheme = link[:8].partition(":")[0].lower()
            row: dict[str, Any] = {"protocol": PREVIEW_PROTOCOLS.get(scheme, "unknown")}
            try:
                prepared = self._prepare_link(link)
            except ValueError as exc:
                row["error"] = str(exc)
            else:
                url = prepared.pop("url")
                row.update(prepared)
                if row["protocol"] == "http":
                    row["name"] = urlsplit(url).hostname
                key = _link_key(url)
                if key in seen:
                    row["error"] = "与前面的链接重复"
                seen.add(key)
            rows.append(row)
        return rows

    def start(self, uploads: UploadService, links: list[str]) -> None:
        if not 1 <= len(links) <= 2:
            raise ValueError("每批最多导入两个视频")
        jobs = []
        seen: set[str] = set()
        for link in links:
            job = self._prepare_link(link)
            key = _link_key(job["url"])
            if key in seen:
                raise ValueError("同一批不能导入重复的链接")
            seen.add(key)
            job.update(id=uuid.uuid4().hex, state="queued", downloaded=0)
            jobs.append(job)
        self._start_batch(uploads, jobs)

    def _inbox_downloader(self) -> QBittorrent:
        if self.qb is None:
            raise ValueError("服务器尚未启用 qBittorrent")
        return self.qb

    async def inbox(self) -> dict:
        if self.qb is None:
            return {"enabled": False, "tasks": []}
        tasks = await self.qb.inbox(self.inbox_directory)
        importing = (
            {job.get("torrent_hash") for job in self.batch["jobs"] if job["state"] not in TERMINAL}
            if self.batch
            else set()
        )
        for task in tasks:
            task["importing"] = task["hash"] in importing
        return {"enabled": True, "tasks": tasks}

    async def start_inbox(self, uploads: UploadService, digest: str) -> None:
        tasks = await self._inbox_downloader().inbox(self.inbox_directory)
        task = next((task for task in tasks if task["hash"] == digest and task["ready"]), None)
        if task is None:
            raise ValueError("下载任务不存在或尚未完成")
        self._start_batch(
            uploads,
            [
                dict(
                    id=uuid.uuid4().hex,
                    state="queued",
                    torrent_hash=digest,
                    name=task["name"],
                    downloaded=task["size"],
                    size=task["size"],
                )
            ],
        )

    async def delete_inbox(self, digest: str) -> None:
        if self.batch and any(
            job.get("torrent_hash") == digest and job["state"] not in TERMINAL
            for job in self.batch["jobs"]
        ):
            raise RuntimeError("任务正在导入，请先等待完成或取消导入")
        await self._inbox_downloader().delete_inbox(self.inbox_directory, digest)

    def _start_batch(self, uploads: UploadService, jobs: list[dict]) -> None:
        if self.active or (self.worker and not self.worker.done()):
            raise RuntimeError("上一批视频的临时文件尚未清理")
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
        self.progress_events.notify()
        self.worker.add_done_callback(lambda _: self.progress_events.notify())

    async def stop(self) -> None:
        if self.worker and not self.worker.done():
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)

    def cancel(self, uploads: UploadService) -> None:
        if not self.active or not self.batch or self.batch["workspace"] != uploads.db.path:
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
        ed2k_slot = asyncio.Lock()

        async def process(job: dict, index: int) -> None:
            if self.ed2k_port and job.get("url", "").startswith("ed2k:"):
                async with ed2k_slot:
                    await self._process(uploads, directory, job, index, retry)
            else:
                await self._process(uploads, directory, job, index, retry)

        await asyncio.gather(
            *(
                process(job, index)
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
                await self._clean(
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
                if "torrent_hash" not in job:
                    job["downloaded"] = 0
                job.update(state="downloading", error=None)
                job.pop("ed2k_sources", None)
                job.pop("ed2k_network", None)
                job.pop("torrent_seeds", None)
                self._save()
                async with asyncio.timeout(3600):
                    name, mime, size = await self._download(uploads, path, job, index)
                if self.batch["cancel"]:
                    await self._clean(directory, path, job, "cancelled")
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
            await self._clean(directory, path, job, "done")
        except Exception as exc:
            # External URLs may include signed credentials. Never return or log HTTP errors verbatim.
            logger.warning("Video import %s failed (%s)", job["id"], type(exc).__name__)
            if (
                self.qb is not None
                and job.get("url", "").startswith("magnet:")
                and not uploads.db.get_upload(job["id"])
            ):
                try:
                    await self.qb.clean(path)
                    remove_upload_files(path)
                except Exception:
                    logger.warning("Could not clean failed torrent import %s", job["id"])
            job.update(
                state="error",
                error=(
                    str(exc)
                    if type(exc) in {ValueError, UploadSpaceError}
                    else "下载或清理失败，可重试或取消"
                ),
            )
            self._save()

    async def _clean(self, directory: Path, path: Path, job: dict, state: str) -> None:
        if job.get("url", "").startswith("magnet:"):
            if self.qb is None:
                raise ValueError("恢复 qBittorrent 配置后可重试清理")
            await self.qb.clean(path)
        if state == "done" and "torrent_hash" in job:
            await self._inbox_downloader().delete_inbox(
                self.inbox_directory, job["torrent_hash"], missing_ok=True
            )
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
        if "torrent_hash" in job:
            return await self._inbox_downloader().stage_inbox(
                self.inbox_directory, job["torrent_hash"], path, self.max_bytes
            )
        if job["url"].startswith("magnet:"):
            if self.qb is None:
                raise ValueError("服务器尚未启用磁力下载")

            def report_torrent(downloaded: int, size: int, seeds: int) -> None:
                job.update(downloaded=downloaded, size=size, torrent_seeds=seeds)
                self.progress_events.notify()

            return await self.qb.download(
                job["url"],
                path,
                uploads.space,
                self.max_bytes,
                lambda: self.cancelling,
                report_torrent,
            )
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
                self.progress_events.notify()

            def report_network(status: dict[str, str]) -> None:
                job["ed2k_network"] = status
                self.progress_events.notify()

            return await download_ed2k(
                job["url"],
                path,
                self.ed2k_seeds,
                uploads.space,
                self.max_bytes,
                lambda: self.cancelling,
                report_progress,
                fixed_port=self.ed2k_port,
                network_status=report_network,
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
                    self.progress_events.notify()
                if (
                    self.batch
                    and not self.batch["cancel"]
                    and (not size or (expected is not None and size != expected))
                ):
                    raise ValueError("视频为空或下载不完整")
            return name, mime, size
