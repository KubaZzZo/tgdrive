from __future__ import annotations

import asyncio
import os
import re
import secrets
import shutil
import socket
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote, unquote

from app.infrastructure.db.common import clean_name
from app.services.uploads import UploadSpace
from app.services.video_source import VIDEO_TYPES

FILE_LINK = re.compile(
    r"ed2k://(?P<separator>\||%7c|\\\|)file(?P=separator)"
    r"(?P<name>[^|]+?)(?P=separator)(?P<size>[0-9]{1,19})(?P=separator)"
    r"(?P<hash>[a-f0-9]{32})(?P=separator)/",
    re.IGNORECASE | re.ASCII,
)
SEED_FILES = ("server.met", "nodes.dat", "staticservers.dat")


def parse_link(url: str) -> tuple[str, int, str, str]:
    match = FILE_LINK.fullmatch(url.strip().removesuffix("\\"))
    if not match:
        raise ValueError("需要标准 eD2k 文件链接")
    try:
        name = clean_name(unquote(match["name"], errors="strict"))
        name_length = len(name.encode())
    except UnicodeError:
        raise ValueError("eD2k 文件名编码无效") from None
    if name_length > 255 or any(ord(char) < 32 or char in '|:*?"<>\x7f' for char in name):
        raise ValueError("eD2k 文件名含非法字符")
    mime = VIDEO_TYPES.get(Path(name).suffix.lower())
    size = int(match["size"])
    if mime is None or size <= 0:
        raise ValueError("eD2k 链接必须指向有效的视频文件")
    normalized = f"ed2k://|file|{quote(name, safe='')}|{size}|{match['hash'].upper()}|/"
    return name, size, mime, normalized


def work_dir(path: Path) -> Path:
    return path.with_suffix(".amule")


def _ports() -> tuple[int, int, int]:
    with (
        socket.socket() as tcp,
        socket.socket(type=socket.SOCK_DGRAM) as udp,
        socket.socket() as ec,
    ):
        tcp.bind(("", 0))
        udp.bind(("", 0))
        ec.bind(("127.0.0.1", 0))
        return tcp.getsockname()[1], udp.getsockname()[1], ec.getsockname()[1]


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.wait()


async def _download_progress(
    amulecmd: str, directory: Path, digest: str, size: int
) -> tuple[int, int] | None:
    process = await asyncio.create_subprocess_exec(
        amulecmd,
        "-c",
        "show dl",
        cwd=directory,
        env={**os.environ, "LC_ALL": "C"},
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(2):
            stdout, _ = await process.communicate()
    except TimeoutError:
        return None
    finally:
        await _stop_process(process)
    if process.returncode != 0:
        return None
    # aMule 2.3.3 prints the hash and filename, then the percentage and source counts.
    match = re.search(
        rf"^ > {digest} [^\n]+\n > [ \t]+\[([0-9]+\.[0-9]+)%\][ \t]+[0-9]+/[ \t]*([0-9]+)",
        stdout.decode(errors="replace"),
        re.IGNORECASE | re.MULTILINE,
    )
    if match is None:
        return None
    # The percentage is rounded. Only the verified file can reach the full byte count.
    downloaded = min(size - 1, int(size * float(match[1]) / 100))
    return downloaded, int(match[2])


async def download_ed2k(
    url: str,
    path: Path,
    seeds: Path,
    space: UploadSpace,
    max_bytes: int,
    cancelled: Callable[[], bool],
    progress: Callable[[int, int | None], None],
) -> tuple[str, str, int]:
    name, size, mime, link = parse_link(url)
    if size > max_bytes:
        raise ValueError("视频超过导入大小上限")
    directory = work_dir(path)
    if directory.exists():
        shutil.rmtree(directory)
    path.unlink(missing_ok=True)
    async with space.reserve(path, size):
        try:
            incoming = directory / "Incoming"
            incoming.mkdir(parents=True, mode=0o700)
            temporary = directory / "Temp"
            temporary.mkdir(mode=0o700)
            copied = False
            for filename in SEED_FILES:
                seed = seeds / filename
                if seed.is_file():
                    shutil.copyfile(seed, directory / filename)
                    copied = True
            if not copied:
                raise ValueError("eD2k 下载需要管理员配置 aMule 节点或服务器列表")
            port, udp_port, ec_port = _ports()
            ec_password = secrets.token_hex(16)
            # aMule 2.3.3 resolves amulecmd's configuration relative to ./config.
            command_config = directory / "config"
            command_config.mkdir(mode=0o700)
            (command_config / "remote.conf").write_text(
                f"[EC]\nHost=127.0.0.1\nPort={ec_port}\nPassword={ec_password}\n",
                encoding="utf-8",
            )
            (directory / "amule.conf").write_text(
                "[eMule]\n"
                f"Port={port}\nUDPPort={udp_port}\n"
                f"IncomingDir={incoming}\nTempDir={temporary}\n"
                "Autoconnect=1\nConnectToED2K=1\nConnectToKad=1\n"
                "AddServerListFromServer=0\nAddServerListFromClient=0\n"
                "Serverlist=0\nFilterLanIPs=1\nCheckDiskspace=1\n"
                f"MinFreeDiskSpace={space.min_free_bytes // (1024**2)}\n"
                "[ExternalConnect]\nAcceptExternalConnections=1\nECAddress=127.0.0.1\n"
                f"ECPort={ec_port}\nECPassword={ec_password}\n"
                "[WebServer]\nEnabled=0\n",
                encoding="utf-8",
            )
            (directory / "ED2KLinks").write_text(link + "\n", encoding="utf-8")
            try:
                process = await asyncio.create_subprocess_exec(
                    "amuled",
                    "-c",
                    str(directory),
                    cwd=directory,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            except FileNotFoundError:
                raise ValueError("服务器尚未安装 aMuled") from None
            completed = incoming / name
            amulecmd = shutil.which("amulecmd")
            digest = link.split("|")[4]
            try:
                while not cancelled():
                    if (
                        completed.is_file()
                        and not completed.is_symlink()
                        and completed.stat().st_size == size
                    ):
                        completed.replace(path)
                        progress(size, None)
                        return name, mime, size
                    if process.returncode is not None:
                        raise ValueError("aMuled 已退出，请检查下载器配置")
                    if amulecmd:
                        current = await _download_progress(amulecmd, directory, digest, size)
                        if current is not None:
                            progress(*current)
                    await asyncio.sleep(2)
                return name, mime, 0
            finally:
                await _stop_process(process)
        finally:
            if directory.exists():
                shutil.rmtree(directory)
