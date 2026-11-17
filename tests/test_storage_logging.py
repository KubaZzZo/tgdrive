from __future__ import annotations

import asyncio
import io
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import make_settings

from app.core.logging import JsonFormatter
from app.infrastructure import storage as module


@pytest.mark.parametrize("level", [logging.INFO, logging.WARNING])
def test_connected_message_uses_json_logger_and_respects_level(workdir, monkeypatch, capsys, level):
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(JsonFormatter())
    logger = logging.Logger("tgdrive.storage", level=level)
    logger.addHandler(handler)
    monkeypatch.setattr(module, "logger", logger)
    client = SimpleNamespace(
        connect=AsyncMock(),
        get_me=AsyncMock(return_value=SimpleNamespace(username="test-account", id=42)),
        get_entity=AsyncMock(return_value=object()),
        disconnect=AsyncMock(),
    )
    monkeypatch.setattr(module, "TelegramClient", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(module, "get_proxy", lambda: None)
    monkeypatch.setattr(module.utils, "get_peer_id", lambda _entity: -100123)
    storage = module.TelegramStorage(make_settings(workdir))

    async def run():
        await storage.start()
        assert storage._started
        await storage.stop()

    asyncio.run(run())
    assert capsys.readouterr().out == ""
    if level == logging.INFO:
        record = json.loads(output.getvalue())
        assert record["level"] == "INFO"
        assert record["logger"] == "tgdrive.storage"
        assert record["message"] == "Telegram connected mode=user account=test-account"
        assert record["ts"]
    else:
        assert output.getvalue() == ""
