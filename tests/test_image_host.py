from __future__ import annotations

import asyncio

import pytest

from app.db import Database
from app.services.image_host import MAX_IMAGE_SIZE, image_mime

PNG = b"\x89PNG\r\n\x1a\n" + b"test-image" * 8


def image(app, storage, data=PNG, name="photo.png"):
    message = len(storage.files) + 800
    storage.files[message] = data
    return app.state.db.create_file(
        name,
        None,
        len(data),
        "image/png",
        None,
        str(storage.chat),
        [{"part_index": 0, "tg_message_id": message, "size": len(data)}],
    )


def test_publish_anonymous_range_revoke_and_republish(authenticated_client):
    client, app, storage = authenticated_client
    item = image(app, storage)
    assert client.get(f"/i/{item['id']}").status_code == 404
    published = client.post(f"/api/images/{item['id']}")
    assert published.status_code == 200
    url = published.json()["url"]
    assert client.post(f"/api/images/{item['id']}").json()["url"] == url
    assert client.get("/api/images?q=photo").json()["total"] == 1
    assert client.get("/api/images?q=missing").json()["total"] == 0
    assert client.get("/api/images?offset=1").json()["images"] == []
    client.cookies.clear()
    assert client.get("/api/images").status_code == 401
    assert client.post(f"/api/images/{item['id']}").status_code == 401
    assert client.delete(f"/api/images/{item['id']}").status_code == 401
    assert client.get(f"/api/files/{item['id']}/download").status_code == 401
    response = client.get(url)
    assert response.status_code == 200 and response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["access-control-allow-origin"] == "*"
    assert client.head(url).headers["content-length"] == str(len(PNG))
    response = client.get(url, headers={"Range": "bytes=8-15"})
    assert response.status_code == 206 and response.content == PNG[8:16]
    client.post("/api/login", json={"password": "correct-horse-battery-staple"})
    assert client.delete(f"/api/images/{item['id']}").status_code == 200
    assert client.get(url).status_code == 404
    assert app.state.db.get_item(item["id"]) is not None
    new_url = client.post(f"/api/images/{item['id']}").json()["url"]
    assert new_url != url
    assert client.get(url).status_code == 404
    client.delete(f"/api/items/{item['id']}")
    assert client.get(new_url).status_code == 404


@pytest.mark.parametrize(
    "data", [b"<svg onload=alert(1) />", b"<html>evil</html>", b"not an image"]
)
def test_fake_images_cannot_be_published(authenticated_client, data):
    client, app, storage = authenticated_client
    item = image(app, storage, data)
    assert client.post(f"/api/images/{item['id']}").status_code == 400
    assert client.get("/api/images").json()["total"] == 0


def test_channel_media_replacement_revokes_image_link_but_caption_edits_do_not(
    authenticated_client,
):
    client, app, storage = authenticated_client
    db = app.state.db
    chat = str(storage.chat)
    original = {
        "name": "photo.png",
        "size": len(PNG),
        "mime": "image/png",
        "created_at": "2026-01-01T00:00:00+00:00",
        "media_key": "photo-1",
        "remote_version": "2026-01-01T00:00:00+00:00",
    }
    storage.files[900] = PNG
    db.import_channel_file(chat, 900, original)
    item = db.list_items(None)[0]
    url = client.post(f"/api/images/{item['id']}").json()["url"]
    db.import_channel_file(
        chat,
        900,
        {
            **original,
            "name": "caption.png",
            "remote_version": "2026-01-02T00:00:00+00:00",
        },
    )
    assert client.get(url).content == PNG
    replacement = b"PRIVATE_DOCUMENT_CONTENT"
    storage.files[900] = replacement
    db.import_channel_file(
        chat,
        900,
        {
            **original,
            "name": "private.txt",
            "mime": "text/plain",
            "size": len(replacement),
            "media_key": "document-2",
            "remote_version": "2026-01-03T00:00:00+00:00",
        },
    )
    client.cookies.clear()
    assert client.get(url).status_code == 404
    assert db.get_item(item["id"])["name"] == "private.txt"
    assert db.list_hosted_images("", 24, 0) == ([], 0)


@pytest.mark.parametrize("event", ["tagged_edit", "untagged_edit", "delete", "bot_edit"])
def test_web_upload_message_changes_revoke_publication(authenticated_client, event):
    from test_media import message

    client, app, storage = authenticated_client
    item = image(app, storage)
    url = client.post(f"/api/images/{item['id']}").json()["url"]
    download = f"/api/files/{item['id']}/download"
    old_tag = client.head(download).headers["etag"]
    mid = app.state.db.get_parts(item["id"])[0]["tg_message_id"]
    storage.invalidate_message = lambda _: None
    if event == "delete":
        asyncio.run(app.state.channel_sync.on_delete([mid]))
    elif event == "bot_edit":
        from types import SimpleNamespace

        app.state.channel_sync._ingest_bot_post(
            SimpleNamespace(id="bot"),
            {
                "message_id": mid,
                "edit_date": 123,
                "caption": "tgdrive:v1:test:0",
            },
        )
    else:
        caption = "tgdrive:v1:test:0" if event == "tagged_edit" else "replacement"
        asyncio.run(app.state.channel_sync.on_edit(message(mid, "private.txt", caption=caption)))
    assert client.head(download).headers["etag"] != old_tag
    client.cookies.clear()
    assert client.get(url).status_code == 404
    assert app.state.db.get_item(item["id"]) is not None


def test_upload_auto_publishes_only_image_host_jobs(authenticated_client):
    client, app, storage = authenticated_client
    for host in (False, True):
        created = client.post(
            "/api/uploads",
            json={
                "name": "photo.png",
                "size": len(PNG),
                "mime": "text/html",
                "image_host": host,
            },
        ).json()
        upload_id = created["id"]
        for offset in range(0, len(PNG), created["chunk_size"]):
            assert (
                client.patch(
                    f"/api/uploads/{upload_id}",
                    content=PNG[offset : offset + created["chunk_size"]],
                    headers={"Upload-Offset": str(offset)},
                ).status_code
                == 200
            )
        # Run finalization deterministically without a second Telegram client.
        asyncio.run(app.state.uploads._process(upload_id))
        assert client.get(f"/api/uploads/{upload_id}").json()["state"] == "done"
        assert client.get("/api/images").json()["total"] == int(host)
    result = client.get("/api/images").json()["images"][0]
    assert client.get(result["url"]).headers["content-type"] == "image/png"


def test_image_upload_validation_happens_before_telegram(authenticated_client):
    client, app, storage = authenticated_client
    assert (
        client.post(
            "/api/uploads", json={"name": "big.png", "size": MAX_IMAGE_SIZE + 1, "image_host": True}
        ).status_code
        == 413
    )
    created = client.post(
        "/api/uploads", json={"name": "fake.png", "size": 3, "image_host": True}
    ).json()
    client.patch(f"/api/uploads/{created['id']}", content=b"bad", headers={"Upload-Offset": "0"})
    asyncio.run(app.state.uploads._process(created["id"]))
    assert client.get(f"/api/uploads/{created['id']}").json()["state"] == "error"
    assert storage.files == {}


def test_image_management_rejects_a_stale_workspace(authenticated_client):
    client, app, storage = authenticated_client
    item = image(app, storage)
    for method, path in (
        ("GET", "/api/images"),
        ("POST", f"/api/images/{item['id']}"),
        ("DELETE", f"/api/images/{item['id']}"),
    ):
        assert (
            client.request(method, path, headers={"X-Workspace-ID": "old-channel"}).status_code
            == 409
        )


def test_public_image_retains_cdn_protection(authenticated_client):
    from unittest.mock import AsyncMock, patch

    from fastapi import HTTPException

    client, app, storage = authenticated_client
    item = image(app, storage)
    url = client.post(f"/api/images/{item['id']}").json()["url"]
    with patch.object(app.state.cdn, "check", AsyncMock(side_effect=HTTPException(429, "quota"))):
        assert client.get(url).status_code == 429


def test_schema10_upgrade_preserves_files_uploads_and_links(workdir):
    path = workdir / "upgrade-image.sqlite3"
    db = Database(path)
    upload = db.create_upload(None, "keep.png", 12, "image/png", "temp", 8)
    with db.conn:
        db.conn.execute("DROP TABLE image_hosts")
        db.conn.execute("ALTER TABLE uploads DROP COLUMN image_host")
        db.conn.execute("PRAGMA user_version=10")
    db.close()
    db = Database(path)
    assert db.get_upload(upload["id"])["image_host"] == 0
    assert db.list_hosted_images("", 24, 0) == ([], 0)
    db.close()


@pytest.mark.parametrize(
    "header,mime",
    [
        (PNG, "image/png"),
        (b"\xff\xd8\xff123", "image/jpeg"),
        (b"GIF89a123", "image/gif"),
        (b"RIFF1234WEBP123", "image/webp"),
    ],
)
def test_raster_signatures(header, mime):
    assert image_mime(header) == mime
