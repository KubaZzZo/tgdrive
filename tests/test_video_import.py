from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from app.services import ed2k_source, video_import
from app.services.torrent_source import CATEGORY, INBOX_CATEGORY, QBittorrent, parse_magnet
from app.services.uploads import UploadSpace
from app.services.video_source import public_address, validate_url


class Source:
    headers = {"content-type": "video/mp4", "content-length": "12"}

    async def aiter_raw(self, _size):
        yield b"test fixture"


@asynccontextmanager
async def video(url):
    yield Source(), url


def test_qb_inbox_uploads_before_deleting_and_excludes_other_downloads(
    authenticated_client, monkeypatch
):
    client, app, storage = authenticated_client
    service = app.state.video_import
    service.qb = QBittorrent("http://qb.example", "admin", "fixture")
    inbox = service.inbox_directory
    inbox.mkdir(parents=True)
    video_path = inbox / "fixture.mp4"
    video_path.write_bytes(b"test fixture")
    digest = "1" * 40
    unmanaged = "2" * 40
    tasks = {
        digest: {
            "hash": digest,
            "category": INBOX_CATEGORY,
            "save_path": str(inbox),
            "content_path": str(video_path),
            "name": video_path.name,
            "state": "stoppedUP",
            "progress": 1,
            "amount_left": 0,
            "completed": 12,
            "total_size": 12,
        },
        unmanaged: {
            "hash": unmanaged,
            "category": INBOX_CATEGORY,
            "save_path": str(inbox.parent),
            "content_path": str(inbox.parent / "unrelated.mp4"),
        },
    }

    def qbit(request):
        action = request.url.path.rsplit("/", 1)[-1]
        if action == "login":
            return httpx.Response(204, headers={"set-cookie": "SID=test; Path=/"})
        if action == "info":
            requested = request.url.params.get("hashes")
            return httpx.Response(
                200, json=[task for key, task in tasks.items() if requested in (None, key)]
            )
        if action == "files":
            assert request.url.params["hash"] == digest
            return httpx.Response(200, json=[{"name": video_path.name, "size": 12, "progress": 1}])
        if action == "delete":
            assert parse_qs(request.content.decode()) == {
                "hashes": [digest],
                "deleteFiles": ["true"],
            }
            assert list(storage.files.values()) == [b"test fixture"]
            video_path.unlink()
            del tasks[digest]
            return httpx.Response(200)
        pytest.fail(f"unexpected qBittorrent action {action}")

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: real_client(*args, **kwargs, transport=httpx.MockTransport(qbit)),
    )
    endpoint = "/api/video-import/downloads"
    assert [task["hash"] for task in client.get(endpoint).json()["tasks"]] == [digest]
    assert client.delete(f"{endpoint}/{unmanaged}").status_code == 422
    assert client.post(f"{endpoint}/{digest}/import").status_code == 202

    async def settle():
        await asyncio.wait_for(service.worker, 5)

    client.portal.call(settle)
    assert client.get("/api/video-import").json()["jobs"][0]["state"] == "done"
    assert not video_path.exists()
    assert list(tasks) == [unmanaged]


def test_magnet_import_retries_cleanup_after_telegram_commit(authenticated_client, monkeypatch):
    client, app, storage = authenticated_client
    service = app.state.video_import
    downloader = QBittorrent("http://qb.example", "admin", "fixture")
    service.qb = downloader
    digest = "0123456789abcdef0123456789abcdef01234567"
    link = f"magnet:?xt=urn:btih:{digest}&dn=fixture.mp4&tr=udp%3A%2F%2Fexample.com"
    assert parse_magnet(link) == (f"magnet:?xt=urn:btih:{digest}", "fixture.mp4")
    owned = {}
    deleted = []

    def qbit(request):
        action = request.url.path.rsplit("/", 1)[-1]
        data = {key: value[0] for key, value in parse_qs(request.content.decode()).items()}
        if action == "login":
            return httpx.Response(204, headers={"set-cookie": "SID=test; Path=/"})
        if action == "stop":
            return httpx.Response(200, text="Ok.")
        if action == "version":
            return httpx.Response(200, text="v5.3.0")
        if action == "categories":
            return httpx.Response(200, json={CATEGORY: {}})
        if action == "add":
            assert data["stopCondition"] == "MetadataReceived"
            assert data["urls"] == parse_magnet(link)[0]
            owned.update(
                tags=data["tags"],
                category=CATEGORY,
                hash=digest,
                save_path=data["savepath"],
                state="stoppedDL",
                total_size=12,
                amount_left=12,
                progress=0,
                completed=0,
                num_seeds=1,
            )
            return httpx.Response(200, text="Ok.")
        if action == "info":
            manual = {"tags": "", "hash": "0" * 40, "category": "", "save_path": "/manual"}
            return httpx.Response(200, json=[manual, owned] if owned else [manual])
        if action == "files":
            return httpx.Response(200, json=[{"name": "fixture.mp4", "size": 12, "index": 0}])
        if action == "start":
            (Path(owned["save_path"]) / "fixture.mp4").write_bytes(b"test fixture")
            owned.update(state="uploading", amount_left=0, progress=1, completed=12)
            return httpx.Response(200)
        if action == "delete":
            assert data == {"hashes": digest, "deleteFiles": "false"}
            assert list(storage.files.values()) == [b"test fixture"]
            assert (Path(owned["save_path"]) / "fixture.mp4").exists()
            deleted.append(digest)
            if len(deleted) == 1:
                return httpx.Response(503)
            owned.clear()
            return httpx.Response(200)
        pytest.fail(f"unexpected qBittorrent action {action}")

    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        return real_client(*args, **kwargs, transport=httpx.MockTransport(qbit))

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    endpoint = "/api/video-import"

    async def settle():
        await asyncio.wait_for(service.worker, 8)

    assert client.post(endpoint, json={"links": [link]}).status_code == 202
    client.portal.call(settle)
    assert client.get(endpoint).json()["jobs"][0]["state"] == "done"
    assert not list(service.directory.iterdir())
    assert len(deleted) == 2
    assert list(storage.files.values()) == [b"test fixture"]


def test_preview_marks_duplicate_and_unsupported_links_without_starting_download(
    authenticated_client, monkeypatch, workdir
):
    client, app, storage = authenticated_client
    service = app.state.video_import
    service.ed2k_seeds = workdir
    link = "ed2k://|file|first.mp4|12|A549B3E090C89D60D45FECD957EBCA27|/"
    duplicate = link.replace("first.mp4", "renamed.mp4").replace("|", "%7C")
    other_version = link.replace("A549B3E0", "A549B3E1")
    endpoint = "/api/video-import"

    def unexpected_download(*_args, **_kwargs):
        pytest.fail("preview must not fetch a link")

    monkeypatch.setattr(video_import, "open_video", unexpected_download)
    monkeypatch.setattr(video_import, "download_ed2k", unexpected_download)
    response = client.post(
        endpoint + "/preview",
        json={
            "links": [
                link,
                duplicate,
                other_version,
                "magnet:?xt=urn:btih:example",
                "https://example.com/video.mp4?token=private",
                "invalid link",
                "x" * 4097,
            ]
        },
    )
    assert response.status_code == 200
    rows = response.json()["rows"]
    assert len(rows) == 7
    assert rows[0] == {"protocol": "ed2k", "name": "first.mp4", "size": 12}
    assert rows[1] == {
        "protocol": "ed2k",
        "name": "renamed.mp4",
        "size": 12,
        "error": "与前面的链接重复",
    }
    assert rows[2] == rows[0]
    assert rows[3] == {"protocol": "magnet", "error": "服务器尚未启用磁力下载"}
    assert rows[4] == {"protocol": "http", "name": "example.com"}
    assert rows[5]["protocol"] == rows[6]["protocol"] == "unknown"
    assert all("error" in row for row in rows[5:])
    assert "private" not in response.text

    response = client.post(endpoint, json={"links": [link, duplicate]})
    assert response.status_code == 422
    assert response.json()["detail"] == "同一批不能导入重复的链接"
    assert service.batch is None
    assert service.worker is None
    assert not storage.files


@pytest.mark.parametrize("length", ["12", None])
def test_http_import_reports_progress_before_download_completes(
    authenticated_client, monkeypatch, length
):
    client, app, _ = authenticated_client
    partial = asyncio.Event()
    finish = asyncio.Event()

    class StreamingVideo(Source):
        headers = {"content-type": "video/mp4"}
        if length is not None:
            headers["content-length"] = length

        async def aiter_raw(self, _size):
            yield b"test "
            partial.set()
            await finish.wait()
            yield b"fixture"

    @asynccontextmanager
    async def stream(url):
        yield StreamingVideo(), url

    async def wait_for_partial():
        await asyncio.wait_for(partial.wait(), timeout=2)

    async def wait_for_completion():
        finish.set()
        await asyncio.wait_for(app.state.video_import.worker, timeout=2)

    monkeypatch.setattr(video_import, "open_video", stream)
    endpoint = "/api/video-import"
    assert (
        client.post(endpoint, json={"links": ["https://example.com/video.mp4"]}).status_code == 202
    )
    client.portal.call(wait_for_partial)
    job = client.get(endpoint).json()["jobs"][0]
    assert job["state"] == "downloading"
    assert job["downloaded"] == 5
    assert job.get("size") == (12 if length else None)

    client.portal.call(wait_for_completion)
    job = client.get(endpoint).json()["jobs"][0]
    assert (job["state"], job["downloaded"], job["size"]) == ("done", 12, 12)


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


def test_ed2k_removes_work_files_when_daemon_fails(tmp_path, monkeypatch):
    class FailedDaemon:
        returncode = 1

        async def wait(self):
            return self.returncode

    path = tmp_path / "upload_fixture.part"

    async def spawn(*args, cwd, **kwargs):
        (cwd / "Temp" / "001.part").write_bytes(b"partial")
        return FailedDaemon()

    monkeypatch.setattr(ed2k_source.asyncio, "create_subprocess_exec", spawn)
    (tmp_path / "server.met").touch()
    link = "ed2k://|file|fixture.mp4|12|A549B3E090C89D60D45FECD957EBCA27|/"
    with pytest.raises(ValueError, match="aMuled 已退出"):
        asyncio.run(
            ed2k_source.download_ed2k(
                link,
                path,
                tmp_path,
                UploadSpace(min_free_mib=0),
                100,
                lambda: False,
                lambda *_: None,
            )
        )
    assert not ed2k_source.work_dir(path).exists()
    assert not path.exists()


def test_ed2k_reads_progress_for_the_requested_hash(tmp_path, monkeypatch):
    digest = "A549B3E090C89D60D45FECD957EBCA27"

    class QueueResponse:
        returncode = 0

        async def communicate(self):
            return (
                b" > 00000000000000000000000000000000 unrelated.mp4\n"
                b" > \t [0.0%]    0/   0          - Waiting\n"
                + f" > {digest.lower()} fixture.mp4\n".encode()
                + b" > \t [12.3%]    2/   5 (01) - Downloading\n",
                None,
            )

        async def wait(self):
            return self.returncode

    async def spawn(*args, **kwargs):
        assert args[-2:] == ("-c", "show dl")
        assert kwargs["cwd"] == tmp_path
        return QueueResponse()

    monkeypatch.setattr(ed2k_source.asyncio, "create_subprocess_exec", spawn)
    assert asyncio.run(ed2k_source._download_progress("amulecmd", tmp_path, digest, 1000)) == (
        123,
        5,
    )


@pytest.mark.parametrize(
    ("server", "kad", "expected"),
    [
        (
            "Connected to test [192.0.2.1:5555] with LowID",
            "Connected (firewalled)",
            {"server": "low_id", "kad": "firewalled"},
        ),
        (
            "Connected to test [192.0.2.1:5555] with HighID",
            "Connected (ok)",
            {"server": "high_id", "kad": "open"},
        ),
        ("Now connecting", "Not running", {"server": "connecting", "kad": "offline"}),
        ("Not connected", "Not connected", {"server": "offline", "kad": "offline"}),
    ],
)
def test_ed2k_reports_download_network_state(tmp_path, monkeypatch, server, kad, expected):
    async def status(cli, directory, command):
        assert command == "status"
        return f" > eD2k: {server}\n > Kad: {kad}\n > Total sources:\t0\n"

    monkeypatch.setattr(ed2k_source, "_amule_command", status)
    assert asyncio.run(ed2k_source._network_status("amulecmd", tmp_path)) == expected


def test_ed2k_fixed_port_keeps_second_job_queued(authenticated_client, monkeypatch, workdir):
    client, app, _ = authenticated_client
    service = app.state.video_import
    service.ed2k_seeds = workdir
    service.ed2k_port = 4662
    links = [f"ed2k://|file|fixture{index}.mp4|12|{index:032x}|/" for index in (1, 2)]
    order = []

    async def download(url, path, seeds, space, max_bytes, cancelled, progress, **options):
        assert options["fixed_port"] == 4662
        name, size, mime, _ = ed2k_source.parse_link(url)
        order.append(("start", name))
        await asyncio.sleep(0)
        order.append(("finish", name))
        path.write_bytes(b"test fixture")
        return name, mime, size

    monkeypatch.setattr(video_import, "download_ed2k", download)
    assert client.post("/api/video-import", json={"links": links}).status_code == 202

    async def settle():
        await service.worker

    client.portal.call(settle)
    assert order == [
        ("start", "fixture1.mp4"),
        ("finish", "fixture1.mp4"),
        ("start", "fixture2.mp4"),
        ("finish", "fixture2.mp4"),
    ]


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


@pytest.mark.parametrize(
    ("separator", "suffix"), [("|", ""), ("|", "\\"), ("%7C", ""), (r"\|", "")]
)
def test_ed2k_batch_uses_same_upload_and_cleanup(
    authenticated_client, monkeypatch, workdir, separator, suffix
):
    client, app, storage = authenticated_client
    service = app.state.video_import
    service.ed2k_seeds = workdir
    link = "ed2k://|file|fixture.mp4|12|A549B3E090C89D60D45FECD957EBCA27|/"

    async def download(url, path, seeds, space, max_bytes, cancelled, progress, **options):
        from app.services.ed2k_source import parse_link, work_dir

        assert url == link
        name, size, mime, _ = parse_link(url)
        work_dir(path).mkdir()
        (work_dir(path) / "001.part.met").write_bytes(b"temporary metadata")
        path.write_bytes(b"test fixture")
        progress(size, None)
        return name, mime, size

    monkeypatch.setattr(video_import, "download_ed2k", download)
    endpoint = "/api/video-import"
    assert (
        client.post(endpoint, json={"links": [link + "|sources,127.0.0.1:80|/"]}).status_code == 422
    )
    accepted = client.post(endpoint, json={"links": [link.replace("|", separator) + suffix]})
    assert accepted.status_code == 202
    assert accepted.json()["jobs"][0]["name"] == "fixture.mp4"
    assert accepted.json()["jobs"][0]["size"] == 12

    async def settle():
        await service.worker

    client.portal.call(settle)
    assert client.get(endpoint).json()["jobs"][0]["state"] == "done"
    assert list(storage.files.values()) == [b"test fixture"]
    assert not list(service.directory.iterdir())


def test_ed2k_recognizes_large_video():
    link = "ed2k://|file|SNOS-141.mp4|6800928646|A549B3E090C89D60D45FECD957EBCA27|/"
    assert ed2k_source.parse_link(link) == ("SNOS-141.mp4", 6800928646, "video/mp4", link)
    with pytest.raises(ValueError, match="文件名编码无效"):
        ed2k_source.parse_link(link.replace("SNOS-141", "%FF"))
