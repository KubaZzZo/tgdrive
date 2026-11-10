from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from conftest import FakeTelegramStorage, make_settings
from fastapi.testclient import TestClient

from app.main import create_app

PASSWORD = "correct-horse-battery-staple"


def test_health_auth_and_request_id(app_client):
    client, app, storage = app_client

    live = client.get("/api/health/live")
    assert live.status_code == 200
    assert live.json() == {"status": "ok"}
    assert live.headers.get("X-Request-ID")
    assert live.headers.get("X-Content-Type-Options") == "nosniff"
    assert "default-src 'self'" in live.headers.get("Content-Security-Policy", "")

    ready = client.get("/api/health/ready")
    assert ready.status_code == 200
    assert ready.json()["checks"] == {"database": "ok", "telegram": "ok"}

    assert client.get("/api/items").status_code == 401
    wrong = client.post("/api/login", json={"password": "wrong"})
    assert wrong.status_code == 401
    assert wrong.json()["detail"] == "密码错误"

    ok = client.post("/api/login", json={"password": PASSWORD})
    assert ok.status_code == 200
    assert client.get("/api/meta").json()["authenticated"] is True
    assert client.get("/api/items").status_code == 200


def test_login_rate_limit(workdir):
    settings = make_settings(workdir, login_max_attempts=2)
    app = create_app(settings, storage=FakeTelegramStorage())
    with TestClient(app) as client:
        assert client.post("/api/login", json={"password": "bad"}).status_code == 401
        assert client.post("/api/login", json={"password": "bad"}).status_code == 401
        blocked = client.post("/api/login", json={"password": PASSWORD})
        assert blocked.status_code == 429
        assert int(blocked.headers["Retry-After"]) >= 1


def test_folder_lifecycle(authenticated_client):
    client, app, storage = authenticated_client
    folder = client.post("/api/folders", json={"name": "videos", "parent_id": None})
    assert folder.status_code == 200
    folder_id = folder.json()["id"]

    child = client.post("/api/folders", json={"name": "2026", "parent_id": folder_id})
    assert child.status_code == 200
    child_id = child.json()["id"]

    renamed = client.patch(f"/api/items/{child_id}", json={"name": "2025"})
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "2025"

    moved = client.patch(
        f"/api/items/{child_id}",
        json={"move": True, "parent_id": None},
    )
    assert moved.status_code == 200
    assert moved.json()["parent_id"] is None

    items = client.get("/api/items").json()["items"]
    assert {item["name"] for item in items} == {"videos", "2025"}

    deleted = client.delete(f"/api/items/{folder_id}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] == 1


def test_resumable_upload_download_share_and_delete(authenticated_client):
    client, app, storage = authenticated_client

    created = client.post(
        "/api/uploads",
        json={"name": "hello.txt", "size": 10, "mime": "text/plain", "parent_id": None},
    )
    assert created.status_code == 200
    payload = created.json()
    upload_id = payload["id"]
    assert payload["chunk_size"] == 8
    assert "temp_path" not in payload

    first = client.patch(
        f"/api/uploads/{upload_id}",
        headers={"Upload-Offset": "0", "Content-Type": "application/octet-stream"},
        content=b"hello",
    )
    assert first.status_code == 200
    assert first.json()["offset"] == 5

    duplicate = client.patch(
        f"/api/uploads/{upload_id}",
        headers={"Upload-Offset": "0", "Content-Type": "application/octet-stream"},
        content=b"hello",
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True

    mismatch = client.patch(
        f"/api/uploads/{upload_id}",
        headers={"Upload-Offset": "7", "Content-Type": "application/octet-stream"},
        content=b"x",
    )
    assert mismatch.status_code == 409

    second = client.patch(
        f"/api/uploads/{upload_id}",
        headers={"Upload-Offset": "5", "Content-Type": "application/octet-stream"},
        content=b"world",
    )
    assert second.status_code == 200
    assert second.json()["offset"] == 10

    completed = client.post(f"/api/uploads/{upload_id}/complete")
    assert completed.status_code == 202

    upload = None
    for _ in range(100):
        upload = client.get(f"/api/uploads/{upload_id}").json()
        if upload["state"] in {"done", "error"}:
            break
        time.sleep(0.02)
    assert upload is not None
    assert upload["state"] == "done", upload
    assert "temp_path" not in upload

    items = client.get("/api/items").json()["items"]
    assert len(items) == 1
    item = items[0]
    assert item["name"] == "hello.txt"
    assert item["size"] == 10

    checked = client.get(f"/api/files/{item['id']}/download/check")
    assert checked.status_code == 204
    assert not checked.content
    storage.stopped = True
    try:
        failed_check = client.get(f"/api/files/{item['id']}/download/check")
        assert failed_check.status_code == 503
        assert failed_check.json()["detail"] == "Telegram 尚未连接"
    finally:
        storage.stopped = False

    download = client.get(f"/api/files/{item['id']}/download?download=1")
    assert download.status_code == 200
    assert download.content == b"helloworld"
    assert download.headers["accept-ranges"] == "bytes"

    partial = client.get(
        f"/api/files/{item['id']}/download",
        headers={"Range": "bytes=2-6"},
    )
    assert partial.status_code == 206
    assert partial.content == b"llowo"
    assert partial.headers["content-range"] == "bytes 2-6/10"

    invalid_range = client.get(
        f"/api/files/{item['id']}/download",
        headers={"Range": "bytes=99-100"},
    )
    assert invalid_range.status_code == 416
    assert invalid_range.headers["content-range"] == "bytes */10"

    share = client.post(f"/api/items/{item['id']}/share", json={"expires_hours": 1})
    assert share.status_code == 200
    token = share.json()["token"]
    client.post("/api/logout")

    page = client.get(f"/s/{token}")
    assert page.status_code == 200
    assert "hello.txt" in page.text
    shared_download = client.get(f"/s/{token}/download")
    assert shared_download.status_code == 200
    assert shared_download.content == b"helloworld"

    client.post("/api/login", json={"password": PASSWORD})
    deleted = client.delete(f"/api/items/{item['id']}")
    assert deleted.status_code == 200
    assert storage.deleted_message_ids == [101]


def test_upload_rejects_wrong_chunk_size(authenticated_client):
    client, app, storage = authenticated_client
    payload = client.post(
        "/api/uploads",
        json={"name": "big.bin", "size": 20, "mime": "application/octet-stream"},
    ).json()
    upload_id = payload["id"]
    response = client.patch(
        f"/api/uploads/{upload_id}",
        headers={"Upload-Offset": "0", "Content-Type": "application/octet-stream"},
        content=b"123456789",
    )
    assert response.status_code == 413


def test_recover_interrupted_upload(workdir):
    from app.db import Database

    db = Database(workdir / "recover.sqlite3")
    upload = db.create_upload(None, "a.bin", 10, None, str(workdir / "a.part"), 8)
    db.update_upload(upload["id"], state="processing")
    assert db.recover_interrupted_uploads() == 1
    recovered = db.get_upload(upload["id"])
    assert recovered is not None
    assert recovered["state"] == "error"
    assert "重启" in recovered["error"]
    db.close()


def test_ready_reports_storage_failure(workdir):
    class DownStorage(FakeTelegramStorage):
        async def start(self) -> None:
            self.error = "Telegram 未配置"
            self.started = False

    settings = make_settings(workdir)
    app = create_app(settings, storage=DownStorage())
    with TestClient(app) as client:
        response = client.get("/api/health/ready")
        assert response.status_code == 503
        assert response.json()["checks"]["telegram"] == "error"
        assert response.json()["storage_error"] == "Telegram 未配置"


def test_production_settings_reject_weak_secrets(workdir):
    weak = make_settings(workdir, app_env="production", app_secret="short")
    with pytest.raises(RuntimeError):
        weak.validate()


def test_stale_upload_cleanup(workdir):
    from app.db import Database
    from app.services.uploads import UploadService

    db = Database(workdir / "stale.sqlite3")
    temp = workdir / "stale.part"
    temp.write_bytes(b"partial")
    upload = db.create_upload(None, "stale.bin", 10, None, str(temp), 8)
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    with db.conn:
        db.conn.execute("UPDATE uploads SET updated_at=? WHERE id=?", (old, upload["id"]))
    service = UploadService(db, FakeTelegramStorage(), stale_hours=1)
    assert service.cleanup_stale() == 1
    assert not temp.exists()
    assert db.get_upload(upload["id"]) is None
    db.close()


def test_frontend_dist_is_served(workdir):
    dist = workdir / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text(
        '<!doctype html><script type="module" src="/assets/app.js"></script>',
        encoding="utf-8",
    )
    (assets / "app.js").write_text("console.log('ok')", encoding="utf-8")

    app = create_app(make_settings(workdir), storage=FakeTelegramStorage())
    with TestClient(app) as client:
        index = client.get("/")
        assert index.status_code == 200
        assert "/assets/app.js" in index.text
        assert index.headers["cache-control"] == "no-cache"
        asset = client.get("/assets/app.js")
        assert asset.status_code == 200
        assert "console.log" in asset.text
