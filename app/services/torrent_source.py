from __future__ import annotations

import asyncio
import base64
import re
import shutil
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from app.infrastructure.db.common import clean_name
from app.services.uploads import UploadSpace
from app.services.video_source import VIDEO_TYPES

CATEGORY = "tgdrive-import"
INBOX_CATEGORY = "tgdrive-inbox"
TORRENT_ID = re.compile(r"[a-f0-9]{40}|[a-f0-9]{64}")


def parse_magnet(link: str) -> tuple[str, str | None]:
    parts = urlsplit(link)
    if parts.scheme != "magnet" or parts.netloc or parts.path or parts.fragment:
        raise ValueError("磁力链接格式无效")
    fields = parse_qs(parts.query)
    hashes = {}
    for value in fields.get("xt", []):
        method, _, digest = value.lower().rpartition(":")
        if method == "urn:btih":
            if re.fullmatch(r"[a-z2-7]{32}", digest):
                digest = base64.b32decode(digest.upper()).hex()
            if re.fullmatch(r"[a-f0-9]{40}", digest):
                hashes[method] = digest
        if method == "urn:btmh" and re.fullmatch(r"1220[a-f0-9]{64}", digest):
            hashes[method] = digest
    if not hashes:
        raise ValueError("磁力链接缺少有效的 BitTorrent 哈希")
    # Accept only the hash topics. User-supplied trackers, web seeds and peers
    # could make the downloader connect to unintended network destinations.
    query = [("xt", f"{method}:{digest}") for method, digest in sorted(hashes.items())]
    name = fields.get("dn", [""])[0].strip()[:255]
    return "magnet:?" + urlencode(query, safe=":"), name or None


def torrent_dir(path: Path) -> Path:
    return path.with_suffix(".torrent-data")


class QBittorrent:
    def __init__(self, url: str, username: str, password: str):
        self.url = url.rstrip("/")
        self.credentials = {"username": username, "password": password}

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(
            base_url=self.url, headers={"Referer": self.url + "/"}, timeout=10, trust_env=False
        ) as client:
            response = await client.post("/api/v2/auth/login", data=self.credentials)
            response.raise_for_status()
            if not client.cookies or (
                response.status_code != 204 and response.text.strip() != "Ok."
            ):
                raise ValueError("qBittorrent 登录失败")
            yield client

    async def _request(
        self,
        client: httpx.AsyncClient,
        action: str,
        *,
        data: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        if data is None:
            response = await client.get(f"/api/v2/torrents/{action}", params=params)
        else:
            response = await client.post(f"/api/v2/torrents/{action}", data=data)
        response.raise_for_status()
        return response

    async def _task(self, client: httpx.AsyncClient, path: Path) -> dict | None:
        tag = path.stem
        response = await self._request(client, "info", params={"tag": tag})
        for task in response.json():
            if tag not in {part.strip() for part in task["tags"].split(",")}:
                continue
            if (
                task["category"] != CATEGORY
                or Path(task["save_path"]) != torrent_dir(path)
                or not TORRENT_ID.fullmatch(task["hash"])
            ):
                raise ValueError("qBittorrent 任务标记或下载目录不匹配")
            return task
        return None

    @staticmethod
    def _inbox_task(task: dict, directory: Path) -> bool:
        if (
            directory.is_symlink()
            or task["category"] != INBOX_CATEGORY
            or not TORRENT_ID.fullmatch(task["hash"])
            or Path(task["save_path"]) != directory
        ):
            return False
        download_path = task.get("download_path")
        if download_path and Path(download_path) != directory:
            return False
        content = Path(task["content_path"])
        return (
            content != directory
            and content.is_relative_to(directory)
            and (".." not in content.parts and content.resolve() == content)
        )

    @staticmethod
    def _complete(task: dict) -> bool:
        return (
            task["progress"] >= 1
            and task["amount_left"] == 0
            and task["state"]
            not in {
                "error",
                "missingFiles",
                "moving",
                "checkingUP",
                "checkingDL",
                "checkingResumeData",
            }
        )

    async def inbox(self, directory: Path) -> list[dict]:
        async with self._session() as client:
            tasks = await self._request(client, "info", params={"category": INBOX_CATEGORY})
            return [
                dict(
                    hash=task["hash"],
                    name=task["name"],
                    state=task["state"],
                    progress=task["progress"],
                    size=task["total_size"],
                    downloaded=task["completed"],
                    ready=self._complete(task),
                )
                for task in tasks.json()
                if self._inbox_task(task, directory)
            ]

    async def stage_inbox(
        self, directory: Path, digest: str, path: Path, max_bytes: int
    ) -> tuple[str, str, int]:
        async with self._session() as client:
            tasks = (await self._request(client, "info", params={"hashes": digest})).json()
            if not tasks or tasks[0]["hash"] != digest or not self._inbox_task(tasks[0], directory):
                raise ValueError("任务已移除或不在 TG Drive 下载目录")
            task = tasks[0]
            if not self._complete(task):
                raise ValueError("请等待 qBittorrent 完成下载和校验")
            files = (await self._request(client, "files", params={"hash": digest})).json()
            videos = [row for row in files if Path(row["name"]).suffix.lower() in VIDEO_TYPES]
            if len(videos) != 1:
                raise ValueError("下载任务须包含一个视频文件")
            file = videos[0]
            source = directory / file["name"]
            if (
                not source.is_relative_to(directory)
                or ".." in source.parts
                or source.resolve() != source
                or not source.is_file()
                or source.stat().st_size != file["size"]
                or file["progress"] < 1
            ):
                raise ValueError("下载文件路径无效或文件不完整")
            if not 0 < file["size"] <= max_bytes:
                raise ValueError("视频超过导入大小上限或文件为空")
            path.unlink(missing_ok=True)
            path.hardlink_to(source)
            return clean_name(source.name), VIDEO_TYPES[source.suffix.lower()], file["size"]

    async def delete_inbox(self, directory: Path, digest: str, *, missing_ok: bool = False) -> None:
        async with self._session() as client:
            tasks = (await self._request(client, "info")).json()
            task = next((item for item in tasks if item["hash"] == digest), None)
            if task is None and missing_ok:
                return
            if task is None or not self._inbox_task(task, directory):
                raise ValueError("任务已移除或不在 TG Drive 下载目录")
            if task["state"] == "moving":
                raise ValueError("文件正在移动，请稍后再删除")
            content = Path(task["content_path"])
            files = (await self._request(client, "files", params={"hash": digest})).json()
            for file in files:
                path = directory / file["name"]
                if not path.is_relative_to(content) or ".." in path.parts or path.resolve() != path:
                    raise ValueError("下载文件不在对应的任务目录中，请在 qBittorrent 中检查")
            for other in tasks:
                if other is task or not other.get("content_path"):
                    continue
                other_content = Path(other["content_path"]).resolve()
                if content.is_relative_to(other_content) or other_content.is_relative_to(content):
                    raise ValueError("下载路径与其他任务重叠，请在 qBittorrent 中检查")
            await self._request(client, "delete", data={"hashes": digest, "deleteFiles": "true"})
            if (await self._request(client, "info", params={"hashes": digest})).json():
                raise ValueError("qBittorrent 尚未移除任务，请重试")

    async def clean(self, path: Path) -> None:
        for delay in (0, 1, 3):
            await asyncio.sleep(delay)
            try:
                await self._clean_once(path)
            except (httpx.HTTPError, OSError):
                if delay == 3:
                    raise
            else:
                return

    async def _clean_once(self, path: Path) -> None:
        directory = torrent_dir(path)
        if directory.is_symlink():
            raise ValueError("qBittorrent 下载目录无效")
        # Remove data only from the locally derived task directory.
        async with self._session() as client:
            task = await self._task(client, path)
            if task:
                await self._request(
                    client, "delete", data={"hashes": task["hash"], "deleteFiles": "false"}
                )
                if await self._task(client, path):
                    raise ValueError("qBittorrent 任务尚未删除，请重试清理")
        if directory.exists():
            await asyncio.to_thread(shutil.rmtree, directory)

    async def download(
        self,
        link: str,
        path: Path,
        space: UploadSpace,
        max_bytes: int,
        cancelled: Callable[[], bool],
        progress: Callable[[int, int, int], None],
    ) -> tuple[str, str, int]:
        directory = torrent_dir(path)
        if directory.is_symlink():
            raise ValueError("qBittorrent 下载目录无效")
        directory.mkdir(exist_ok=True, mode=0o700)
        async with self._session() as client:
            version = await client.get("/api/v2/app/version")
            version.raise_for_status()
            release = re.match(r"v?(\d+)\.(\d+)", version.text)
            if release is None or tuple(map(int, release.groups())) < (5, 1):
                raise ValueError("磁力导入需要 qBittorrent 5.1 或更高版本")
            task = await self._task(client, path)
            if task is None:
                if CATEGORY not in (await self._request(client, "categories")).json():
                    await self._request(client, "createCategory", data={"category": CATEGORY})
                response = await self._request(
                    client,
                    "add",
                    data={
                        "urls": link,
                        "savepath": str(directory),
                        "autoTMM": "false",
                        "useDownloadPath": "false",
                        "category": CATEGORY,
                        "tags": path.stem,
                        "stopped": "false",
                        "stopCondition": "MetadataReceived",
                    },
                )
                if response.text.strip() == "Fails.":
                    raise ValueError("qBittorrent 无法添加该任务，可能已在下载列表中")

            while not cancelled():
                task = await self._task(client, path)
                if task is not None:
                    if task["state"] in {"error", "missingFiles"}:
                        raise ValueError("qBittorrent 无法读取下载文件")
                    if task["total_size"] > 0:
                        files = (
                            await self._request(client, "files", params={"hash": task["hash"]})
                        ).json()
                        if files:
                            break
                await asyncio.sleep(2)
            else:
                return "", "", 0

            videos = [row for row in files if Path(row["name"]).suffix.lower() in VIDEO_TYPES]
            if len(videos) != 1:
                raise ValueError("磁力链接须包含一个视频文件")
            file = videos[0]
            size = file["size"]
            if not 0 < size <= max_bytes or task["total_size"] > max_bytes:
                raise ValueError("磁力链接超过导入大小上限或文件为空")
            relative = Path(file["name"])
            source = directory / relative
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or not source.resolve().is_relative_to(directory)
            ):
                raise ValueError("磁力下载文件路径无效")
            name = clean_name(relative.name)
            mime = VIDEO_TYPES[relative.suffix.lower()]
            digest = task["hash"]
            if len(files) > 1:
                unwanted = "|".join(str(row["index"]) for row in files if row is not file)
                await self._request(
                    client, "filePrio", data={"hash": digest, "id": unwanted, "priority": "0"}
                )
            path.unlink(missing_ok=True)
            async with space.reserve(path, size):
                resumed = False
                while not cancelled():
                    task = await self._task(client, path)
                    if task is None or task["state"] in {"error", "missingFiles"}:
                        raise ValueError("qBittorrent 下载任务异常")
                    if task["state"] == "stoppedDL" and not resumed:
                        await self._request(client, "start", data={"hashes": digest})
                        resumed = True
                    progress(min(size, task["completed"]), size, task["num_seeds"])
                    if task["amount_left"] == 0 and task["progress"] >= 1:
                        if (
                            source.is_symlink()
                            or not source.is_file()
                            or source.stat().st_size != size
                        ):
                            raise ValueError("磁力下载文件不完整")
                        if not source.resolve().is_relative_to(directory):
                            raise ValueError("磁力下载文件路径无效")
                        await self._request(client, "stop", data={"hashes": digest})
                        path.hardlink_to(source)
                        return name, mime, size
                    await asyncio.sleep(2)
            return name, mime, 0
