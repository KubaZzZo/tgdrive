from __future__ import annotations

import asyncio
import hashlib
import shutil
import subprocess
import sys

import pytest
from conftest import FakeTelegramStorage

from app.infrastructure.db import Database
from app.services import video_prepare
from app.services.uploads import UploadService, UploadSpace, UploadSpaceError
from app.services.video_prepare import mp4_layout, prepared_video, remux_faststart


def box(kind, data=b""):
    return (len(data) + 8).to_bytes(4, "big") + kind + data


def tail_mp4(path):
    path.write_bytes(box(b"ftyp") + box(b"mdat", b"payload") + box(b"moov"))


@pytest.fixture()
def ffmpeg():
    executable = shutil.which("ffmpeg")
    if not executable:
        pytest.skip("FFmpeg is required for real MP4 validation")
    return executable


def test_mp4_layout_skips_fragmented_invalid_and_already_prepared(workdir):
    path = workdir / "video.mp4"
    tail_mp4(path)
    assert mp4_layout(path) == "tail"
    path.write_bytes(box(b"ftyp") + box(b"moov") + box(b"mdat"))
    assert mp4_layout(path) == "faststart"
    path.write_bytes(box(b"ftyp") + box(b"moov") + box(b"moof") + box(b"mdat"))
    assert mp4_layout(path) is None
    path.write_bytes(b"\x00\x00\x00\x10mdatshort")
    assert mp4_layout(path) is None
    path.write_bytes(box(b"ftyp") + b"\0\0\0\1mdat" + (17).to_bytes(8, "big") + b"x" + box(b"moov"))
    assert mp4_layout(path) == "tail"


def test_real_faststart_preserves_streams_and_decodes(workdir, ffmpeg):
    source = workdir / "upload_real.part"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x64:r=12",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-f",
            "mp4",
            "-y",
            str(source),
        ],
        check=True,
    )
    assert mp4_layout(source) == "tail"
    original = source.read_bytes()

    def stream_hash(path):
        return subprocess.check_output(
            [
                ffmpeg,
                "-v",
                "error",
                "-i",
                str(path),
                "-map",
                "0",
                "-c",
                "copy",
                "-f",
                "streamhash",
                "-hash",
                "sha256",
                "-",
            ]
        )

    async def run():
        space = UploadSpace(min_free_mib=0)
        async with prepared_video(source, "video.mp4", space, enabled=True, timeout=10) as output:
            assert output != source and mp4_layout(output) == "faststart"
            assert stream_hash(output) == stream_hash(source)
            subprocess.run(
                [ffmpeg, "-v", "error", "-xerror", "-i", str(output), "-f", "null", "-"], check=True
            )
        assert source.read_bytes() == original and not output.exists()
        assert not space._reservations
        # FFmpeg can exit successfully at -fs; capped output must never be published.
        limited = workdir / "limited.part"
        assert not await remux_faststart(source, limited, 64, 10)

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["missing", "timeout", "invalid", "space"])
def test_prepare_failure_falls_back_and_cleans_output(workdir, monkeypatch, failure):
    source = workdir / "upload_failure.part"
    tail_mp4(source)

    async def remux(path, target, limit, timeout):
        target.write_bytes(b"partial")
        if failure == "missing":
            raise FileNotFoundError("ffmpeg")
        if failure == "timeout":
            raise TimeoutError()
        return False

    monkeypatch.setattr(video_prepare, "remux_faststart", remux)

    async def run():
        space = UploadSpace(max_mib=1 if failure == "space" else 20, min_free_mib=0)
        async with prepared_video(source, "video.mp4", space, enabled=True, timeout=1) as output:
            assert output == source
        assert source.exists() and not space._reservations
        assert not list(workdir.glob("*.faststart.part"))
        # Errors from the caller must propagate once, rather than yielding a fallback again.
        with pytest.raises(UploadSpaceError, match="caller"):
            async with prepared_video(source, "video.mp4", space, enabled=True, timeout=1):
                raise UploadSpaceError("caller")

    asyncio.run(run())


@pytest.mark.parametrize("cancel", [True, False])
def test_remux_cancellation_and_timeout_kill_process(workdir, monkeypatch, cancel):
    source = workdir / "upload_cancel.part"
    tail_mp4(source)
    create = asyncio.create_subprocess_exec
    processes = []

    async def launch(*args, **kwargs):
        target = args[-1]
        process = await create(
            sys.executable,
            "-c",
            "import pathlib,time,sys; pathlib.Path(sys.argv[1]).write_bytes(b'partial'); time.sleep(30)",
            target,
            **kwargs,
        )
        processes.append(process)
        return process

    monkeypatch.setattr(video_prepare.asyncio, "create_subprocess_exec", launch)

    async def run():
        space = UploadSpace(min_free_mib=0)

        async def prepare():
            async with prepared_video(
                source, "video.mp4", space, enabled=True, timeout=10 if cancel else 0.05
            ) as output:
                assert output == source

        task = asyncio.create_task(prepare())
        if cancel:
            async with asyncio.timeout(2):
                while not processes:
                    await asyncio.sleep(0.001)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
        assert processes[0].returncode is not None
        assert source.exists() and not space._reservations
        assert not list(workdir.glob("*.faststart.part"))

    asyncio.run(run())


def test_upload_uses_prepared_size_hash_and_original_progress_on_retry(workdir, monkeypatch):
    source = workdir / "upload_metadata.part"
    tail_mp4(source)
    original = source.read_bytes()
    optimized = box(b"ftyp") + box(b"moov", b"new index") + box(b"mdat", b"payload")

    async def remux(path, target, limit, timeout):
        target.write_bytes(optimized)
        return True

    monkeypatch.setattr(video_prepare, "remux_faststart", remux)

    async def run():
        db = Database(workdir / "metadata.sqlite3")

        class Storage(FakeTelegramStorage):
            async def upload_file(self, **kwargs):
                assert kwargs["path"].read_bytes() == optimized
                await kwargs["progress"](len(optimized) // 2, len(optimized))
                assert db.get_upload(upload["id"])["progress"] <= len(original)
                return await super().upload_file(**kwargs)

        storage = Storage()
        await storage.start()
        service = UploadService(db, storage, space=UploadSpace(min_free_mib=0))
        upload = db.create_upload(None, "video.mp4", len(original), "video/mp4", str(source), 8)
        db.update_upload(upload["id"], received=len(original))
        try:
            with db.conn:
                db.conn.execute("""CREATE TRIGGER fail_completion BEFORE UPDATE OF state ON uploads
                    WHEN NEW.state='done' BEGIN SELECT RAISE(ABORT, 'completion failure'); END""")
            await service._process(upload["id"])
            assert db.get_upload(upload["id"])["state"] == "error"
            assert source.read_bytes() == original and not storage.files
            assert not list(workdir.glob("*.faststart.part"))
            with db.conn:
                db.conn.execute("DROP TRIGGER fail_completion")
            await service._process(upload["id"])
            completed = db.get_upload(upload["id"])
            assert completed["state"] == "done"
            assert (
                completed["progress"] == completed["size"] == completed["received"] == len(original)
            )
            item = db.get_item(completed["item_id"])
            assert item["size"] == len(optimized)
            assert item["sha256"] == hashlib.sha256(optimized).hexdigest()
            assert db.get_parts(item["id"])[0]["size"] == len(optimized)
            assert storage.files == {102: optimized}
            assert not source.exists() and not service.space._reservations
        finally:
            db.close()

    asyncio.run(run())


def test_reserved_output_counts_once_and_blocks_competing_uploads(workdir, monkeypatch):
    from types import SimpleNamespace

    from app.services import uploads

    async def run():
        space = UploadSpace(max_mib=4, min_free_mib=1)
        source = workdir / "upload_source.part"
        target = workdir / "upload_source.faststart.part"
        source.write_bytes(b"x" * (1024 * 1024))
        monkeypatch.setattr(
            uploads.shutil, "disk_usage", lambda _: SimpleNamespace(free=4 * 1024**2)
        )
        async with space.reserve(target, 2 * 1024**2):
            target.write_bytes(b"x" * 1024**2)
            await space.check_admission(workdir, 1024**2)
            with pytest.raises(UploadSpaceError):
                await space.check_admission(workdir, 1024**2 + 1)
            monkeypatch.setattr(
                uploads.shutil, "disk_usage", lambda _: SimpleNamespace(free=2 * 1024**2)
            )
            with pytest.raises(UploadSpaceError):
                await space.check_admission(workdir, 1)
        assert not space._reservations

    asyncio.run(run())


def test_cancellation_waits_for_hash_reader_before_removing_output(workdir, monkeypatch):
    import threading

    from app.services import uploads

    source = workdir / "upload_hash.part"
    tail_mp4(source)
    opened = threading.Event()
    release = threading.Event()
    outputs = []

    async def remux(path, target, limit, timeout):
        target.write_bytes(box(b"ftyp") + box(b"moov") + box(b"mdat", b"payload"))
        return True

    def hash_file(path):
        outputs.append(path)
        with path.open("rb") as file:
            opened.set()
            assert release.wait(5)
            return hashlib.sha256(file.read()).hexdigest()

    monkeypatch.setattr(video_prepare, "remux_faststart", remux)
    monkeypatch.setattr(uploads, "file_sha256", hash_file)

    async def run():
        db = Database(workdir / "hash.sqlite3")
        service = UploadService(db, FakeTelegramStorage(), space=UploadSpace(min_free_mib=0))
        record = db.create_upload(
            None, "video.mp4", source.stat().st_size, "video/mp4", str(source), 8
        )
        task = asyncio.create_task(service._process(record["id"]))
        try:
            async with asyncio.timeout(2):
                while not opened.is_set():
                    await asyncio.sleep(0.001)
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done() and outputs[0].exists()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert source.exists() and not outputs[0].exists() and not service.space._reservations
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            db.close()

    asyncio.run(run())
