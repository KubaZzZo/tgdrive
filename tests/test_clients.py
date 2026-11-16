from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import quote
from xml.etree import ElementTree as XML
from zipfile import ZipFile

from app.infrastructure.db import SCHEMA_VERSION

PNG = b"\x89PNG\r\n\x1a\n" + b"test-image"
DAV = "{DAV:}"


def credential(client, scope):
    result = client.post("/api/clients/credentials", json={"scope": scope, "label": "Laptop"})
    assert result.status_code == 201
    return result.json()


def add_file(db, storage, name, parent=None):
    data = b"abcdef"
    message_id = len(storage.files) + 100
    storage.files[message_id] = data
    return db.create_file(
        name,
        parent,
        len(data),
        "application/octet-stream",
        None,
        str(storage.chat),
        [{"part_index": 0, "size": len(data), "tg_message_id": message_id}],
    )


def test_picgo_upload_publishes_picture_and_credential_is_revocable(authenticated_client):
    client, app, storage = authenticated_client
    issued = credential(client, "picgo")
    headers = {"Authorization": "Bearer " + issued["secret"]}
    files = {"file": ("相片.png", PNG, "image/png")}
    result = client.post("/api/picgo/upload", headers=headers, files=files)
    assert result.is_success, result.text
    assert result.json()["success"]
    link = result.json()["data"]["downloadLink"]
    assert link.startswith("http://testserver/i/")
    image = client.get(link)
    assert image.content == PNG
    assert image.headers["Content-Type"] == "image/png"
    assert list(storage.files.values()) == [PNG]
    assert app.state.db.list_hosted_images("", 10, 0)[0][0]["name"] == "相片.png"
    assert not list(app.state.settings.temp_dir.iterdir())

    rows = client.get("/api/clients/credentials").json()["credentials"]
    assert rows == [{key: value for key, value in issued.items() if key != "secret"}]
    assert client.delete("/api/clients/credentials/" + issued["id"]).is_success
    assert client.post("/api/picgo/upload", headers=headers, files=files).status_code == 401


def test_picgo_rejects_wrong_credentials_incomplete_and_invalid_multipart(authenticated_client):
    client, app, storage = authenticated_client
    url = "/api/picgo/upload"
    dav = credential(client, "webdav")
    issued = credential(client, "picgo")
    headers = {"Authorization": "Bearer " + issued["secret"]}
    assert client.post(url).status_code == 401
    assert client.post(url, headers={"Authorization": f"Bearer {dav['secret']}"}).status_code == 401
    assert (
        client.post(url, headers=headers, files={"file": ("fake.png", b"not PNG")}).status_code
        == 400
    )
    malformed = b'--end\r\nContent-Disposition: form-data; name="file"; filename="a.png"\r\n\r\n'
    assert (
        client.post(
            url,
            headers={**headers, "Content-Type": "multipart/form-data; boundary=end"},
            content=malformed + PNG,
        ).status_code
        == 400
    )
    assert (
        client.post(
            url, headers={**headers, "Content-Length": "30000000"}, files={"file": ("a.png", PNG)}
        ).status_code
        == 413
    )
    storage.chat = -10042
    assert client.post(url, headers=headers, files={"file": ("a.png", PNG)}).status_code == 401
    assert not storage.files
    assert not list(app.state.settings.temp_dir.iterdir())


def test_picgo_failed_telegram_write_does_not_leave_temporary_upload(authenticated_client):
    client, app, storage = authenticated_client

    async def broken(*args, **kwargs):
        raise RuntimeError("Storage temporarily unavailable")

    storage.upload_file = broken
    issued = credential(client, "picgo")
    headers = {"Authorization": f"Bearer {issued['secret']}"}
    result = client.post("/api/picgo/upload", headers=headers, files={"file": ("a.png", PNG)})
    assert result.status_code == 502
    assert not list(app.state.settings.temp_dir.iterdir())
    assert not app.state.db.list_uploads()


def test_readonly_webdav_depth_properties_ranges_and_trash(authenticated_client):
    client, app, storage = authenticated_client
    basic = ("tgdrive", credential(client, "webdav")["secret"])
    db = app.state.db
    folder = db.create_folder("样本 & 资料", None)
    child = add_file(db, storage, "大文件 #1.txt", folder["id"])
    trashed = add_file(db, storage, "hidden.txt", folder["id"])
    db.trash_tree(trashed["id"])
    folder_url = "/webdav/" + quote(folder["name"], safe="") + "/"
    filename = folder_url + quote(child["name"], safe="")
    assert client.request("OPTIONS", "/webdav/").status_code == 401
    assert client.request("OPTIONS", "/webdav/", auth=basic).headers["DAV"] == "1"
    assert client.request("PROPFIND", "/webdav", auth=basic).status_code == 403

    body = b'<D:propfind xmlns:D="DAV:"><D:prop><D:resourcetype/><D:unknown/></D:prop></D:propfind>'
    listing = client.request(
        "PROPFIND", folder_url, headers={"Depth": "1"}, auth=basic, content=body
    )
    assert listing.status_code == 207
    document = XML.fromstring(listing.content)
    assert [node.text for node in document.iter(DAV + "href")] == [folder_url, filename]
    assert document.find(f".//{DAV}collection") is not None
    assert "404 Not Found" in listing.text
    assert (
        client.request(
            "PROPFIND",
            folder_url,
            auth=basic,
            headers={"Depth": "0"},
            content=b'<!DOCTYPE x><propfind xmlns="DAV:" />',
        ).status_code
        == 400
    )
    response = client.get(filename, headers={"Range": "bytes=1-3"}, auth=basic)
    assert response.status_code == 206 and response.content == b"bcd"
    assert response.headers["Content-Range"] == "bytes 1-3/6"
    assert response.headers["Cache-Control"] == "private, no-store"
    assert client.head(filename, auth=basic).headers["Content-Length"] == "6"
    assert client.get(folder_url + "hidden.txt", auth=basic).status_code == 404
    for method in ("PUT", "DELETE", "MKCOL", "MOVE", "COPY", "PROPPATCH"):
        assert client.request(method, filename, auth=basic).status_code == 405
    assert db.get_item(child["id"]) is not None
    storage.chat = -10042
    assert client.get(filename, auth=basic).status_code == 401


def test_webdav_disambiguates_duplicate_names(authenticated_client):
    client, app, storage = authenticated_client
    basic = ("tgdrive", credential(client, "webdav")["secret"])
    db = app.state.db
    first = add_file(db, storage, "same.txt")
    second = add_file(db, storage, "same.txt")
    response = client.request("PROPFIND", "/webdav/", headers={"Depth": "1"}, auth=basic)
    root = XML.fromstring(response.content)
    links = {node.text for node in root.iter(DAV + "href")}
    assert links == {
        "/webdav/",
        f"/webdav/same.txt~{first['id']}",
        f"/webdav/same.txt~{second['id']}",
    }
    assert client.get("/webdav/same.txt", auth=basic).status_code == 404
    assert client.get(f"/webdav/same.txt~{first['id']}", auth=basic).content == b"abcdef"
    db.trash_tree(second["id"])
    assert client.get("/webdav/same.txt", auth=basic).content == b"abcdef"
    assert client.get(f"/webdav/same.txt~{first['id']}", auth=basic).content == b"abcdef"


def test_channel_index_backup_includes_wal_data_but_omits_client_secrets(
    authenticated_client, workdir, monkeypatch
):
    client, app, storage = authenticated_client
    db = app.state.db
    item = add_file(db, storage, "indexed.bin")
    db.create_share(item["id"], "share-token", None)
    db.save_video_import({"url": "private-test-import-url"})
    issued = credential(client, "webdav")
    scratch = workdir / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    monkeypatch.setattr("tempfile.tempdir", str(scratch))
    url = f"/api/clients/index-backup?workspace_id={storage.chat}"
    # A new snapshot is always served whole, even if a client attempts to resume an older one.
    backup = client.get(url, headers={"Range": "bytes=10-20"})
    assert backup.status_code == 200, backup.text
    assert "attachment" in backup.headers["Content-Disposition"]
    assert "Content-Range" not in backup.headers
    assert client.get("/api/clients/index-backup?workspace_id=-10042").status_code == 409
    assert app.state.index_backup_lock.locked() is False
    assert not list(scratch.iterdir())

    with ZipFile(io.BytesIO(backup.content)) as archive:
        assert set(archive.namelist()) == {"manifest.json", "index.sqlite3"}
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["channel_id"] == str(storage.chat)
        assert manifest["source_schema_version"] == SCHEMA_VERSION
        backup_bytes = archive.read("index.sqlite3")
    assert hashlib.sha256(issued["secret"].encode()).hexdigest().encode() not in backup_bytes
    assert b"private-test-import-url" not in backup_bytes
    extracted = Path(workdir / "index.sqlite3")
    extracted.write_bytes(backup_bytes)
    with closing(sqlite3.connect(extracted)) as index:
        assert index.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert index.execute("SELECT name FROM items").fetchall() == [("indexed.bin",)]
        assert index.execute("SELECT token FROM shares").fetchall() == [("share-token",)]
        assert index.execute("SELECT tg_message_id FROM file_parts").fetchall() == [(100,)]
        tables = {
            row[0] for row in index.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert not tables.intersection(
            {"client_credentials", "telegram_profiles", "uploads", "video_import_batch"}
        )

    client.post("/api/logout")
    assert client.get(url).status_code == 401
