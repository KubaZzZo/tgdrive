from __future__ import annotations

import asyncio

import httpx
import pytest
from conftest import FakeTelegramStorage, make_settings
from fastapi.testclient import TestClient

from app.api.routes.google_drive import GoogleDriveResponse
from app.main import create_app


class Media(httpx.AsyncByteStream):
    closed = False

    async def __aiter__(self):
        yield b"234"

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("message_type", ["http.response.start", "http.response.body"])
def test_disconnect_closes_upstream(message_type):
    media = Media()
    upstream = httpx.Response(200, stream=media)
    response = GoogleDriveResponse(upstream, "video/mp4", {})

    async def receive():
        raise AssertionError("ASGI 2.4 streaming should not use receive")

    async def send(message):
        if message["type"] == message_type:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send))
    assert media.closed


def test_private_mount_paths_and_streaming(workdir):
    requests = []
    media = Media()

    async def upstream(request):
        requests.append(request)
        if request.method == "PROPFIND":
            return httpx.Response(
                207,
                text="""<D:multistatus xmlns:D="DAV:">
                <D:response><D:href>/dav/google/</D:href></D:response>
                <D:response><D:href>/dav/google/other/descendant.mp4</D:href></D:response>
                <D:response><D:href>/dav/google/%E7%85%A7%E7%89%87/</D:href>
                    <D:propstat><D:prop><D:resourcetype><D:collection/></D:resourcetype></D:prop>
                    <D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>
                <D:response><D:href>/dav/google/100%25%20%23.mp4</D:href>
                    <D:propstat><D:prop><D:getcontentlength>10</D:getcontentlength></D:prop>
                    <D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>
                </D:multistatus>""",
            )
        if request.url.path.endswith("/redirect.mp4"):
            return httpx.Response(302, headers={"Location": "https://outside.example/token=secret"})
        if request.url.path.endswith("/unsafe.svg"):
            return httpx.Response(200, stream=Media(), headers={"Content-Type": "image/svg+xml"})
        if request.url.path.endswith("/wrong.mp4"):
            return httpx.Response(206, headers={"Content-Range": "bytes 0-2/10"}, stream=Media())
        return httpx.Response(
            206, headers={"Content-Range": "bytes 2-4/10", "Content-Length": "3"}, stream=media
        )

    settings = make_settings(
        workdir,
        google_drive_dav_url="http://openlist.test/dav/google",
        google_drive_dav_username="read-only",
        google_drive_dav_password="private",
    )
    app = create_app(settings, storage=FakeTelegramStorage())
    with TestClient(app) as client:
        drive = app.state.google_drive
        client.portal.call(drive.client.aclose)
        drive.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        base = "/api/google-drive"
        assert client.get(f"{base}/items").status_code == 401
        assert client.get(f"{base}/content", params={"path": "private.mp4"}).status_code == 401
        assert not requests
        assert (
            client.post("/api/login", json={"password": settings.app_password}).status_code == 200
        )
        assert client.get("/api/meta").json()["features"]["google_drive"]
        assert client.get(f"{base}/items", params={"path": "../other"}).status_code == 400
        assert client.get(f"{base}/items", params={"path": "photos\\other"}).status_code == 400
        listing = client.get(f"{base}/items")
        assert listing.status_code == 200
        entries = listing.json()["items"]
        assert entries == client.get(f"{base}/items").json()["items"]
        ids = {entry.pop("id") for entry in entries}
        assert len(ids) == 2 and all(len(identifier) == 64 for identifier in ids)
        assert entries == [
            {"name": "照片", "folder": True, "size": 0, "mime": None},
            {"name": "100% #.mp4", "folder": False, "size": 10, "mime": "video/mp4"},
        ]
        assert requests[-1].headers["depth"] == "1"
        assert requests[-1].headers["authorization"].startswith("Basic ")

        content = client.get(
            f"{base}/content", params={"path": "100% #.mp4"}, headers={"Range": "bytes=2-4"}
        )
        assert requests[-1].url.raw_path == b"/dav/google/100%25%20%23.mp4"
        assert requests[-1].headers["range"] == "bytes=2-4"
        assert content.status_code == 206
        assert content.content == b"234"
        assert content.headers["content-range"] == "bytes 2-4/10"
        assert content.headers["cache-control"] == "private, no-store"
        assert content.headers["content-type"] == "video/mp4"
        assert media.closed

        wrong_range = client.get(
            f"{base}/content", params={"path": "wrong.mp4"}, headers={"Range": "bytes=2-4"}
        )
        assert wrong_range.status_code == 502
        redirect = client.get(f"{base}/content", params={"path": "redirect.mp4"})
        assert redirect.status_code == 502
        assert "location" not in redirect.headers
        assert requests[-1].url.host == "openlist.test"
        unsafe = client.get(f"{base}/content", params={"path": "unsafe.svg"})
        assert unsafe.headers["content-type"] == "application/octet-stream"
        assert unsafe.headers["content-disposition"].startswith("attachment")


def test_mount_disabled_by_default(authenticated_client):
    client, _, _ = authenticated_client
    assert not client.get("/api/meta").json()["features"]["google_drive"]
    assert client.get("/api/google-drive/items").status_code == 404
