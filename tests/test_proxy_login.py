from __future__ import annotations

import json

import pytest
import uvicorn
from conftest import FakeTelegramStorage, make_settings
from fastapi.testclient import TestClient

from app.config import ROOT_DIR
from app.main import create_app

PROXY_IP = "172.30.87.2"
PASSWORD = "correct-horse-battery-staple"


def proxy_client(workdir, monkeypatch, peer=PROXY_IP):
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", PROXY_IP)
    app = create_app(make_settings(workdir, login_max_attempts=2), storage=FakeTelegramStorage())
    # Exercise the same Uvicorn proxy processing used by the Docker entrypoint.
    config = uvicorn.Config(app, proxy_headers=True, log_config=None)
    config.load()
    return TestClient(config.loaded_app, client=(peer, 50000))


@pytest.mark.parametrize("client_ip", ["198.51.100.10", "2001:db8::10"])
def test_trusted_proxy_keeps_login_limits_per_client(workdir, monkeypatch, client_ip):
    with proxy_client(workdir, monkeypatch) as client:
        headers = {"X-Forwarded-For": client_ip}
        for _ in range(2):
            assert (
                client.post("/api/login", json={"password": "bad"}, headers=headers).status_code
                == 401
            )
        blocked = client.post("/api/login", json={"password": PASSWORD}, headers=headers)
        assert blocked.status_code == 429
        assert int(blocked.headers["Retry-After"]) >= 1
        other = {"X-Forwarded-For": "198.51.100.11"}
        assert (
            client.post("/api/login", json={"password": PASSWORD}, headers=other).status_code == 200
        )
        # Another client's successful login must not reset the blocked client's bucket.
        assert (
            client.post("/api/login", json={"password": PASSWORD}, headers=headers).status_code
            == 429
        )


def test_untrusted_peer_cannot_rotate_forwarding_headers(workdir, monkeypatch):
    with proxy_client(workdir, monkeypatch, peer="198.51.100.20") as client:
        for index, expected in enumerate([401, 401, 429]):
            headers = {
                "X-Forwarded-For": f"203.0.113.{index + 1}",
                "X-Real-IP": f"203.0.113.{index + 1}",
            }
            response = client.post("/api/login", json={"password": "bad"}, headers=headers)
            assert response.status_code == expected


def test_trusted_proxy_ignores_spoofed_chain_prefix(workdir, monkeypatch):
    with proxy_client(workdir, monkeypatch) as client:
        for index, expected in enumerate([401, 401, 429]):
            # A proxy appends its actual peer after any incoming forged prefix.
            headers = {"X-Forwarded-For": f"203.0.113.{index + 1}, 198.51.100.20"}
            response = client.post("/api/login", json={"password": "bad"}, headers=headers)
            assert response.status_code == expected


def test_missing_forwarded_for_uses_connection_peer(workdir, monkeypatch):
    with proxy_client(workdir, monkeypatch) as client:
        for index, expected in enumerate([401, 401, 429]):
            response = client.post(
                "/api/login",
                json={"password": "bad"},
                headers={"X-Real-IP": f"203.0.113.{index + 1}"},
            )
            assert response.status_code == expected


def test_docker_entrypoint_preserves_proxy_allowlist_from_environment():
    command_line = next(
        line
        for line in (ROOT_DIR / "Dockerfile").read_text(encoding="utf-8").splitlines()
        if line.startswith("CMD ")
    )
    command = json.loads(command_line.removeprefix("CMD "))
    assert "--proxy-headers" in command
    assert "--forwarded-allow-ips" not in command


@pytest.mark.parametrize("port", [0, 1024, 65525])
def test_ed2k_accepts_valid_port_boundaries(workdir, port):
    make_settings(workdir, video_import_ed2k_port=port).validate()


@pytest.mark.parametrize("port", [-1, 1, 1023, *range(65526, 65537)])
def test_ed2k_rejects_invalid_ports(workdir, port):
    with pytest.raises(RuntimeError, match="VIDEO_IMPORT_ED2K_PORT"):
        make_settings(workdir, video_import_ed2k_port=port).validate()
