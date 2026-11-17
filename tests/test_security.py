from __future__ import annotations

from conftest import FakeTelegramStorage, make_settings
from fastapi.testclient import TestClient

from app.core.security import LoginRateLimiter
from app.infrastructure.db import Database
from app.main import create_app


def test_login_limit_survives_database_reopen_and_expires(workdir, monkeypatch):
    now = [1_800_000_000.0]
    monkeypatch.setattr("app.core.security.time", lambda: now[0])
    path = workdir / "limits.sqlite3"
    db = Database(path)
    limiter = LoginRateLimiter(2, 60, db)
    limiter.record_failure("client-a")
    now[0] += 10
    limiter.record_failure("client-a")
    limiter.record_failure("client-b")
    db.close()

    db = Database(path)
    try:
        limiter = LoginRateLimiter(2, 60, db)
        assert limiter.retry_after("client-a") == 50
        assert limiter.retry_after("client-b") == 0
        now[0] += 50
        assert limiter.retry_after("client-a") == 0
        assert db.conn.execute("SELECT COUNT(*) FROM login_failures").fetchone()[0] == 2
        limiter.record_failure("client-a")
        assert limiter.retry_after("client-a") == 10
        limiter.reset("client-a")
        assert limiter.retry_after("client-a") == 0
        limiter.record_failure("client-b")
        assert limiter.retry_after("client-b") == 10
        now[0] += 60
        assert limiter.retry_after("unused-client") == 0
        assert db.conn.execute("SELECT COUNT(*) FROM login_failures").fetchone()[0] == 0
    finally:
        db.close()


def test_login_limit_survives_application_restart(workdir):
    settings = make_settings(workdir, login_max_attempts=2)
    app = create_app(settings, storage=FakeTelegramStorage())
    with TestClient(app) as client:
        for _ in range(2):
            assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
    restarted = create_app(settings, storage=FakeTelegramStorage())
    with TestClient(restarted) as client:
        response = client.post("/api/login", json={"password": settings.app_password})
        assert response.status_code == 429
        assert int(response.headers["Retry-After"]) > 0
