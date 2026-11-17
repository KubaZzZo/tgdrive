from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from app.infrastructure.db import Database
from app.services.index_backup import build_index_backup
from app.services.index_restore import read_index_archive


@pytest.mark.parametrize("version", [18, 19])
def test_index_backup_excludes_login_records_and_supports_old_schema(workdir, version):
    db = Database(workdir / "source.sqlite3")
    try:
        db.record_login_failure("private-login-client-ip", 1_800_000_000, 0)
        with db.conn:
            db.conn.execute(f"PRAGMA user_version={version}")
        snapshot = workdir / "snapshot"
        snapshot.mkdir()
        archive = build_index_backup(db, snapshot, "-100123", "test")
        with ZipFile(archive) as backup:
            assert b"private-login-client-ip" not in backup.read("index.sqlite3")
        with closing(sqlite3.connect(snapshot / "index.sqlite3")) as reader:
            assert (
                reader.execute(
                    "SELECT name FROM sqlite_schema WHERE name='login_failures'"
                ).fetchone()
                is None
            )
        restore = workdir / "restore"
        restore.mkdir()
        manifest, tables = read_index_archive(archive, restore, "-100123")
        assert manifest["source_schema_version"] == version
        assert "login_failures" not in tables
        assert db.conn.execute("SELECT COUNT(*) FROM login_failures").fetchone()[0] == 1
    finally:
        db.close()


@pytest.mark.parametrize("version", [[19], 20, True])
def test_index_restore_rejects_invalid_schema_versions(workdir, version):
    archive = workdir / "invalid.zip"
    manifest = {
        "format": "tgdrive-channel-index",
        "format_version": 1,
        "source_schema_version": version,
        "app_version": "test",
        "channel_id": "-100123",
    }
    with ZipFile(archive, "w", ZIP_DEFLATED) as backup:
        backup.writestr("manifest.json", json.dumps(manifest))
        backup.writestr("index.sqlite3", b"unused")
    with pytest.raises(ValueError, match="备份版本"):
        read_index_archive(archive, workdir, "-100123")
