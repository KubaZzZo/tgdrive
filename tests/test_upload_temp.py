from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import FakeTelegramStorage, make_settings
from fastapi.testclient import TestClient
from test_video_prepare import box, tail_mp4

from app.infrastructure.db import Database
from app.main import create_app
from app.services import video_prepare
from app.services.upload_temp import faststart_path
from app.services.uploads import UploadService, UploadSpace


def test_startup_removes_crashed_and_orphan_outputs_but_preserves_retry_file(workdir):
    settings = make_settings(workdir)
    app = create_app(settings, storage=FakeTelegramStorage())
    source = settings.temp_dir / "upload_crashed.part"
    source.write_bytes(b"original")
    faststart_path(source).write_bytes(b"partial optimized")
    orphan = settings.temp_dir / "upload_orphan.faststart.part"
    orphan.write_bytes(b"no database row")
    unrelated = settings.temp_dir / "keep.part"
    unrelated.write_bytes(b"unrelated")
    record = app.state.db.create_upload(None, "video.mp4", 8, "video/mp4", str(source), 8)
    app.state.db.update_upload(record["id"], state="processing", received=8)
    with TestClient(app):
        assert source.read_bytes() == b"original"
        assert unrelated.read_bytes() == b"unrelated"
        assert not orphan.exists() and not faststart_path(source).exists()
        recovered = app.state.db.get_upload(record["id"])
        assert recovered["state"] == "error" and recovered["received"] == 8


@pytest.mark.parametrize("asynchronous", [False, True])
def test_stale_cleanup_removes_original_and_crash_sidecar(workdir, asynchronous):
    async def run():
        db = Database(workdir / "stale.sqlite3")
        source = workdir / "upload_stale.part"
        source.write_bytes(b"original")
        faststart_path(source).write_bytes(b"partial optimized")
        record = db.create_upload(None, "video.mp4", 8, "video/mp4", str(source), 8)
        db.update_upload(record["id"], state="error")
        with db.conn:
            db.conn.execute(
                "UPDATE uploads SET updated_at=? WHERE id=?",
                ((datetime.now(UTC) - timedelta(hours=2)).isoformat(), record["id"]),
            )
        service = UploadService(db, FakeTelegramStorage(), stale_hours=1)
        try:
            if asynchronous:
                await service.cleanup_stale_async()
            else:
                assert service.cleanup_stale() == 1
            assert db.get_upload(record["id"]) is None
            assert not source.exists() and not faststart_path(source).exists()
        finally:
            db.close()

    asyncio.run(run())


def test_cancel_upload_removes_crash_sidecar(authenticated_client, workdir):
    client, app, _ = authenticated_client
    source = workdir / "upload_cancel.part"
    source.write_bytes(b"original")
    faststart_path(source).write_bytes(b"partial optimized")
    record = app.state.db.create_upload(None, "video.mp4", 8, "video/mp4", str(source), 8)
    app.state.db.update_upload(record["id"], state="error")
    assert client.delete(f"/api/uploads/{record['id']}").is_success
    assert app.state.db.get_upload(record["id"]) is None
    assert not source.exists() and not faststart_path(source).exists()


def test_workspace_recovery_removes_sidecar_without_discarding_original(workdir):
    db = Database(workdir / "workspace.sqlite3")
    source = workdir / "upload_workspace.part"
    source.write_bytes(b"original")
    faststart_path(source).write_bytes(b"partial optimized")
    record = db.create_upload(None, "video.mp4", 8, "video/mp4", str(source), 8)
    db.update_upload(record["id"], state="processing")
    try:
        service = UploadService(db, FakeTelegramStorage())
        assert service.recover_interrupted() == 1
        assert db.get_upload(record["id"])["state"] == "error"
        assert source.read_bytes() == b"original" and not faststart_path(source).exists()
    finally:
        db.close()


def test_retry_removes_old_output_before_checking_remux_disk_budget(workdir, monkeypatch):
    async def run():
        db = Database(workdir / "retry.sqlite3")
        source = workdir / "upload_retry.part"
        tail_mp4(source)
        faststart_path(source).write_bytes(b"x" * (2 * 1024**2))
        optimized = box(b"ftyp") + box(b"moov") + box(b"mdat", b"payload")
        remuxed = False

        async def remux(path, target, limit, timeout):
            nonlocal remuxed
            assert not target.exists()
            target.write_bytes(optimized)
            remuxed = True
            return True

        monkeypatch.setattr(video_prepare, "remux_faststart", remux)
        storage = FakeTelegramStorage()
        service = UploadService(db, storage, space=UploadSpace(max_mib=9, min_free_mib=0))
        record = db.create_upload(
            None, "video.mp4", source.stat().st_size, "video/mp4", str(source), 8
        )
        db.update_upload(record["id"], state="error", received=source.stat().st_size)
        try:
            await service._process(record["id"])
            assert db.get_upload(record["id"])["state"] == "done"
            assert remuxed and storage.files == {101: optimized}
            assert not source.exists() and not faststart_path(source).exists()
        finally:
            db.close()

    asyncio.run(run())


def test_failed_sidecar_cleanup_preserves_record_for_retry(workdir, monkeypatch):
    async def run():
        db = Database(workdir / "locked.sqlite3")
        source = workdir / "upload_locked.part"
        source.write_bytes(b"original")
        sidecar = faststart_path(source)
        sidecar.write_bytes(b"partial optimized")
        record = db.create_upload(None, "video.mp4", 8, "video/mp4", str(source), 8)
        with db.conn:
            db.conn.execute(
                "UPDATE uploads SET updated_at=? WHERE id=?",
                ((datetime.now(UTC) - timedelta(hours=2)).isoformat(), record["id"]),
            )
        unlink = Path.unlink

        def locked(path, **kwargs):
            if path == sidecar:
                raise PermissionError("output is locked")
            return unlink(path, **kwargs)

        service = UploadService(db, FakeTelegramStorage(), stale_hours=1)
        try:
            with monkeypatch.context() as patch:
                patch.setattr(Path, "unlink", locked)
                with pytest.raises(PermissionError):
                    await service.cleanup_stale_async()
            assert db.get_upload(record["id"]) is not None and source.exists() and sidecar.exists()
            await service.cleanup_stale_async()
            assert (
                db.get_upload(record["id"]) is None and not source.exists() and not sidecar.exists()
            )
        finally:
            db.close()

    asyncio.run(run())
