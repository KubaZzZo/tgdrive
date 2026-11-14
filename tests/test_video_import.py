from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest

from app.services import video_import
from app.services.video_source import public_address, validate_url


class Source:
    headers = {"content-type": "video/mp4", "content-length": "12"}

    async def aiter_raw(self, _size):
        yield b"test fixture"


@asynccontextmanager
async def video(url):
    yield Source(), url


def test_link_batch_keeps_next_batch_locked_until_cleanup(authenticated_client, monkeypatch):
    client, app, storage = authenticated_client
    monkeypatch.setattr(video_import, "open_video", video)
    service = app.state.video_import
    original = video_import.remove_upload_files

    def refuse_cleanup(path):
        raise OSError("fixture")

    monkeypatch.setattr(video_import, "remove_upload_files", refuse_cleanup)
    endpoint = "/api/video-import"
    links = {"links": ["https://example.com/a.mp4", "https://example.com/b.mp4"]}
    assert client.post(endpoint, json={"links": links["links"] * 2}).status_code == 422
    assert client.post(endpoint, json=links).status_code == 202

    async def settle():
        await service.worker

    client.portal.call(settle)
    result = client.get(endpoint).json()
    assert result["active"]
    assert all(job["state"] == "error" and "url" not in job for job in result["jobs"])
    assert client.post(endpoint, json=links).status_code == 409
    assert len(storage.files) == 2

    monkeypatch.setattr(video_import, "remove_upload_files", original)
    assert client.post(endpoint + "/retry").status_code == 202
    client.portal.call(settle)
    assert [job["state"] for job in client.get(endpoint).json()["jobs"]] == ["done", "done"]
    assert not list(service.directory.iterdir())
    assert len(storage.files) == 2
    assert len(app.state.db.list_items(None)) == 2
    assert not client.get("/api/uploads").json()["uploads"]


def test_failed_download_can_be_cancelled(authenticated_client, monkeypatch):
    client, app, _ = authenticated_client

    @asynccontextmanager
    async def unavailable(url):
        raise httpx.ConnectError("private signed URL")
        yield

    monkeypatch.setattr(video_import, "open_video", unavailable)
    service = app.state.video_import
    endpoint = "/api/video-import"
    assert (
        client.post(endpoint, json={"links": ["https://example.com/clip.mp4"]}).status_code == 202
    )

    async def settle():
        await service.worker

    client.portal.call(settle)
    assert client.get(endpoint).json()["active"]
    assert "signed URL" not in client.get(endpoint).text
    assert client.post(endpoint + "/cancel").status_code == 202
    client.portal.call(settle)
    assert not client.get(endpoint).json()["active"]
    assert not list(service.directory.iterdir())


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1"])
def test_import_rejects_non_public_addresses(address):
    async def run():
        with pytest.raises(ValueError, match="公网"):
            await public_address(validate_url(str(httpx.URL(scheme="http", host=address))))

    asyncio.run(run())


def test_interrupted_batch_resumes_on_restart(workdir, monkeypatch):
    from conftest import FakeTelegramStorage, make_settings
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings = make_settings(workdir)
    interrupted = create_app(settings, storage=FakeTelegramStorage())

    @asynccontextmanager
    async def wait_for_shutdown(url):
        await asyncio.Event().wait()
        yield

    monkeypatch.setattr(video_import, "open_video", wait_for_shutdown)
    endpoint = "/api/video-import"
    with TestClient(interrupted) as client:
        client.post("/api/login", json={"password": settings.app_password})
        assert (
            client.post(endpoint, json={"links": ["https://example.com/clip.mp4"]}).status_code
            == 202
        )

    monkeypatch.setattr(video_import, "open_video", video)
    storage = FakeTelegramStorage()
    restarted = create_app(settings, storage=storage)
    with TestClient(restarted) as client:
        client.post("/api/login", json={"password": settings.app_password})

        async def settle():
            await restarted.state.video_import.worker

        client.portal.call(settle)
        assert client.get(endpoint).json()["jobs"][0]["state"] == "done"
        assert list(storage.files.values()) == [b"test fixture"]
        assert not list(restarted.state.video_import.directory.iterdir())
