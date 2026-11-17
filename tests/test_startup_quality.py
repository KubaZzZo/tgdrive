from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from unittest.mock import Mock

import pytest
import uvicorn
from conftest import FakeTelegramStorage, make_settings
from fastapi.testclient import TestClient

from app import config, main
from app.config import ROOT_DIR
from scripts import chat_id, login_qr


def test_import_does_not_initialize_runtime(workdir):
    targets = {
        "DATA_DIR": str(workdir / "data"),
        "TEMP_DIR": str(workdir / "uploads"),
        "DB_PATH": str(workdir / "db" / "index.sqlite3"),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import logging
import os
from pathlib import Path

handlers = list(logging.getLogger().handlers)
from app import config
assert not any(Path(os.environ[key]).exists() for key in ('DATA_DIR', 'TEMP_DIR', 'DB_PATH'))
from app import main
assert callable(main.create_app)
assert not hasattr(main, 'app')
assert logging.getLogger().handlers == handlers
assert not any(Path(os.environ[key]).exists() for key in ('DATA_DIR', 'TEMP_DIR', 'DB_PATH'))
""",
        ],
        cwd=ROOT_DIR,
        env={
            **os.environ,
            **targets,
            "APP_ENV": "production",
            "APP_PASSWORD": "change-me",
            "APP_SECRET": "change-this-secret-in-env",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert not any(workdir.iterdir())


def test_uvicorn_factory_initializes_application_explicitly(workdir, monkeypatch):
    settings = make_settings(workdir)
    applications = []
    factory = main.create_app

    def create():
        application = factory(settings, storage=FakeTelegramStorage())
        applications.append(application)
        return application

    monkeypatch.setattr(main, "create_app", create)
    server = uvicorn.Config("app.main:create_app", factory=True)
    server.load()
    assert server.loaded
    assert len(applications) == 1
    assert settings.db_path.is_file()
    with TestClient(applications[0]) as client:
        assert client.get("/api/health/live").status_code == 200


@pytest.mark.parametrize("environment", ["development", "test", "staging", "production", "prod"])
@pytest.mark.parametrize("credential", ["app_password", "app_secret"])
def test_default_credentials_warn_in_every_environment(
    workdir, monkeypatch, environment, credential
):
    logger = Mock()
    monkeypatch.setattr(config, "logger", logger)
    defaults = {"app_password": "change-me", "app_secret": "change-this-secret-in-env"}
    settings = make_settings(workdir, app_env=environment, **{credential: defaults[credential]})
    if settings.is_production:
        with pytest.raises(RuntimeError, match=credential.upper()):
            settings.validate()
    else:
        settings.validate()
    logger.warning.assert_called_once()
    message = logger.warning.call_args.args[0]
    assert credential.upper() in message
    assert defaults[credential] not in message


def test_private_credentials_do_not_generate_warnings(workdir, monkeypatch):
    logger = Mock()
    monkeypatch.setattr(config, "logger", logger)
    make_settings(workdir, app_env="production").validate()
    logger.warning.assert_not_called()


def test_startup_security_warnings_use_json_logging(workdir, capsys):
    settings = make_settings(
        workdir,
        app_env="production",
        app_password="change-me",
        app_secret="change-this-secret-in-env",
        log_json=True,
    )
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        with pytest.raises(RuntimeError, match="APP_SECRET"):
            main.create_app(settings)
        records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert len(records) == 2
        assert all(record["level"] == "WARNING" for record in records)
        assert all(record["logger"] == "tgdrive.config" for record in records)
        assert "APP_PASSWORD" in records[0]["message"]
        assert "APP_SECRET" in records[1]["message"]
        assert not settings.data_dir.exists()
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)


@pytest.mark.parametrize("script", [login_qr, chat_id])
def test_login_tools_create_directories_at_runtime(workdir, monkeypatch, script):
    settings = make_settings(workdir, tg_session=workdir / "sessions" / "custom")
    monkeypatch.setattr(script, "settings", settings)

    def client(*_args, **_kwargs):
        assert settings.data_dir.is_dir()
        assert settings.temp_dir.is_dir()
        assert settings.tg_session.parent.is_dir()
        raise RuntimeError("client constructor reached")

    monkeypatch.setattr(script, "TelegramClient", client)
    with pytest.raises(RuntimeError, match="client constructor reached"):
        asyncio.run(script.main())


def test_workdir_is_owned_by_pytest(workdir, tmp_path):
    assert workdir == tmp_path


def test_all_supported_launch_commands_use_explicit_factory():
    docker_line = next(
        line
        for line in (ROOT_DIR / "Dockerfile").read_text(encoding="utf-8").splitlines()
        if line.startswith("CMD ")
    )
    command = json.loads(docker_line.removeprefix("CMD "))
    assert command[1:3] == ["app.main:create_app", "--factory"]
    for path in ["Makefile", "README.md", "deploy/tgdrive.service"]:
        text = (ROOT_DIR / path).read_text(encoding="utf-8")
        assert "app.main:create_app --factory" in text
