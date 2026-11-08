from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import ROOT_DIR, Settings
from app.main import create_app


class FakeTelegramStorage:
    def __init__(self) -> None:
        self.chat = -1001234567890
        self.error: str | None = None
        self.started = False
        self.stopped = False
        self.files: dict[int, bytes] = {}
        self.deleted_message_ids: list[int] = []
        self._next_id = 100

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def status(self) -> dict[str, Any]:
        return {
            "configured": True,
            "connected": self.started and not self.stopped,
            "mode": "user",
            "error": self.error,
            "chat_id": str(self.chat),
            "segment_size": 1024,
        }

    async def upload_file(self, path: Path, name: str, mime=None, progress=None, storage_tag=None):
        data = path.read_bytes()
        self._next_id += 1
        message_id = self._next_id
        self.files[message_id] = data
        if progress:
            await progress(len(data), len(data))
        return [{"part_index": 0, "tg_message_id": message_id, "size": len(data)}]

    async def iter_download(self, message_id: int, offset: int, length: int):
        data = self.files[message_id][offset : offset + length]
        for index in range(0, len(data), 3):
            yield data[index : index + 3]

    async def delete_messages(self, message_ids: list[int]) -> None:
        self.deleted_message_ids.extend(message_ids)
        for message_id in message_ids:
            self.files.pop(message_id, None)


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_name": "TG Drive Test",
        "app_env": "test",
        "data_dir": tmp_path / "data",
        "temp_dir": tmp_path / "tmp",
        "db_path": tmp_path / "data" / "test.sqlite3",
        "frontend_dist": tmp_path / "dist",
        "traffic_host_state": tmp_path / "host.json",
        "axisnow_api_token": "",
        "app_password": "correct-horse-battery-staple",
        "app_secret": "test-secret-" + "x" * 40,
        "cookie_secure": False,
        "tg_mode": "user",
        "tg_api_id": 12345,
        "tg_api_hash": "test-api-hash",
        "tg_chat_id": "-1001234567890",
        "tg_segment_size": 1024,
        "upload_chunk_size": 8,
        "max_file_size": 0,
        "login_max_attempts": 5,
        "login_window_seconds": 60,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture()
def workdir() -> Path:
    path = ROOT_DIR / "tmp" / "tests" / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture()
def app_client(workdir: Path):
    storage = FakeTelegramStorage()
    settings = make_settings(workdir)
    app = create_app(settings, storage=storage)
    with TestClient(app) as client:
        yield client, app, storage


@pytest.fixture()
def authenticated_client(app_client):
    client, app, storage = app_client
    response = client.post("/api/login", json={"password": "correct-horse-battery-staple"})
    assert response.status_code == 200
    return client, app, storage
