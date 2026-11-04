from __future__ import annotations

import sqlite3
import time

import pytest

from app.categories import classify_file, effective_mime
from app.db import SCHEMA, SCHEMA_VERSION, Database


@pytest.mark.parametrize(
    "name,mime,expected",
    [
        ("IMG_001.HEIC", None, "image"),
        ("IMG_002.heif", "application/octet-stream", "image"),
        ("相机.MOV", "", "video"),
        ("拍摄.mp4", "application/octet-stream", "video"),
        ("录音.m4a", None, "audio"),
        ("报告.docx", "application/zip", "document"),
        ("旅行.pdf", None, "document"),
        ("照片.tar.gz", None, "archive"),
        ("main.ts", "text/plain", "code"),
        ("手机.apk", None, "application"),
        ("no-extension", "image/jpeg", "image"),
        ("unknown.blob", None, "other"),
    ],
)
def test_mobile_and_desktop_classification(name, mime, expected):
    assert classify_file(name, mime) == expected


def legacy_database(path):
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO items (id,kind,name,size,created_at,updated_at) VALUES ('old','file','旧照片.HEIC',10,'2026-01-01','2026-01-01')"
        )
        conn.execute("PRAGMA user_version=1")


def test_existing_database_is_classified_without_moving_files(workdir):
    path = workdir / "legacy.sqlite3"
    legacy_database(path)
    db = Database(path)
    item = db.get_item("old")
    assert item["category"] == "image"
    assert item["parent_id"] is None
    assert db.schema_version() == SCHEMA_VERSION
    db.close()
    db = Database(path)
    assert db.get_item("old")["category"] == "image"
    db.close()


def test_failed_migration_rolls_back_schema_for_retry(workdir, monkeypatch):
    path = workdir / "rollback.sqlite3"
    legacy_database(path)
    with monkeypatch.context() as patch:

        def fail(*args):
            raise RuntimeError("backfill interrupted")

        patch.setattr("app.db.classify_file", fail)
        with pytest.raises(RuntimeError, match="backfill interrupted"):
            Database(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert "category" not in [row[1] for row in conn.execute("PRAGMA table_info(items)")]
    db = Database(path)
    assert db.get_item("old")["category"] == "image"
    db.close()


def add_file(db, name, parent=None, size=1):
    return db.create_file(name, parent, size, None, "sha", "-100123", [])


def test_category_query_search_sort_and_pagination(authenticated_client):
    client, app, _ = authenticated_client
    db = app.state.db
    folder = db.create_folder("旅行", None)
    nested = add_file(db, "Photo.HEIC", folder["id"], 20)
    add_file(db, "截图.png", None, 10)
    add_file(db, "100%_plan.pdf", None, 50)
    add_file(db, "other.pdf", None, 5)
    response = client.get(
        "/api/items",
        params={
            "scope": "all",
            "category": "image",
            "sort": "size",
            "direction": "desc",
            "limit": 1,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2 and data["items"][0]["id"] == nested["id"]
    assert data["items"][0]["parent_id"] == folder["id"]
    assert {c["id"]: c["count"] for c in data["categories"]}["document"] == 2
    second = client.get(
        "/api/items",
        params={
            "scope": "all",
            "category": "image",
            "sort": "size",
            "direction": "desc",
            "limit": 1,
            "offset": 1,
        },
    ).json()
    assert second["items"][0]["name"] == "截图.png"
    literal = client.get("/api/items", params={"scope": "all", "q": "%_"}).json()
    assert literal["total"] == 1 and literal["items"][0]["name"] == "100%_plan.pdf"
    mixed_case = client.get("/api/items", params={"scope": "all", "q": "pHoTo"}).json()
    assert mixed_case["total"] == 1
    scoped = client.get("/api/items", params={"parent_id": folder["id"]}).json()
    assert len(scoped["items"]) == 1
    recent = client.get(
        "/api/items", params={"scope": "recent", "sort": "created_at", "direction": "desc"}
    ).json()
    assert all(item["kind"] == "file" for item in recent["items"])
    updated = client.patch(f"/api/items/{nested['id']}", json={"name": "converted.pdf"}).json()
    assert updated["category"] == "document"
    assert client.get("/api/items", params={"category": "invalid"}).status_code == 422
    assert client.get("/api/items", params={"limit": 201}).status_code == 422
    assert client.get("/api/items", params={"sort": "name; DROP TABLE items"}).status_code == 422


@pytest.mark.parametrize(
    "name,mime,category,agent",
    [
        ("电脑照片.png", "image/png", "image", "Desktop browser"),
        ("手机照片.HEIC", "application/octet-stream", "image", "iPhone browser"),
        ("手机录像.MOV", None, "video", "Android browser"),
    ],
)
def test_uploads_classify_on_server_for_every_device(
    authenticated_client, name, mime, category, agent
):
    client, app, _ = authenticated_client
    folder = client.post("/api/folders", json={"name": "保留目录"}).json()
    response = client.post(
        "/api/uploads",
        json={"name": name, "size": 4, "mime": mime, "parent_id": folder["id"]},
        headers={"User-Agent": agent},
    )
    assert response.status_code == 200
    uid = response.json()["id"]
    assert (
        client.patch(
            f"/api/uploads/{uid}", content=b"data", headers={"Upload-Offset": "0"}
        ).status_code
        == 200
    )
    assert client.post(f"/api/uploads/{uid}/complete").status_code == 202
    for _ in range(100):
        record = client.get(f"/api/uploads/{uid}").json()
        if record["state"] in {"done", "error"}:
            break
        time.sleep(0.01)
    assert record["state"] == "done", record
    item = app.state.db.get_item(record["item_id"])
    assert item["category"] == category
    assert item["parent_id"] == folder["id"]
    files = client.get("/api/items", params={"scope": "all", "category": category}).json()
    assert files["items"][0]["id"] == item["id"]


def test_generic_mobile_mime_is_inferred_for_preview():
    assert effective_mime("phone.MP4", "application/octet-stream") == "video/mp4"
    assert effective_mime("photo.JPG", None) == "image/jpeg"
    assert effective_mime("report.pdf", "") == "application/pdf"
    assert effective_mime("recording", "audio/ogg") == "audio/ogg"
    assert effective_mime("unknown.blob", None) == "application/octet-stream"
