from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from app.infrastructure.db import migrations


def schema_seven():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    for version, upgrade in migrations._MIGRATIONS:
        if version <= 7:
            upgrade(conn)
    return conn


def test_migration_versions_are_contiguous_and_advance_individually():
    assert [version for version, _ in migrations._MIGRATIONS] == list(
        range(1, migrations.SCHEMA_VERSION + 1)
    )
    with closing(schema_seven()) as conn:
        for version, upgrade in migrations._MIGRATIONS:
            if version > 7:
                upgrade(conn)
                assert conn.execute("PRAGMA user_version").fetchone()[0] == version


@pytest.mark.parametrize("variant", ["7", "8-cdn", "8-bot", "9-bot", "9-cdn"])
def test_legacy_migration_paths_preserve_data_and_finish_idempotently(variant):
    with closing(schema_seven()) as conn:
        with conn:
            conn.execute(
                "INSERT INTO items(id,kind,name,created_at,updated_at) VALUES (?,?,?,?,?)",
                ("kept", "file", "kept.bin", "2026-11-17", "2026-11-17"),
            )
        if "bot" in variant:
            migrations._upgrade_to_8(conn)
            if variant.startswith("9"):
                migrations._upgrade_to_9(conn)
            with conn:
                conn.execute(
                    "INSERT INTO channel_bot_files VALUES (?,?,?,?,?)",
                    ("kept", "bot", "file-id", "version", 5),
                )
                if variant.startswith("9"):
                    conn.execute(
                        "INSERT INTO channel_bot_covers VALUES (?,?)", ("kept", "cover-id")
                    )
        elif "cdn" in variant:
            with conn:
                conn.execute(
                    "CREATE TABLE cdn_usage (uuid TEXT, period TEXT, charged INTEGER, "
                    "PRIMARY KEY(uuid, period))"
                )
                conn.execute("INSERT INTO cdn_usage VALUES ('sub', '2026-11', 123)")
        with conn:
            conn.execute(f"PRAGMA user_version={int(variant[0])}")
        migrations.migrate(conn)
        migrations.migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == migrations.SCHEMA_VERSION
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT name FROM items").fetchone()[0] == "kept.bin"
        if "cdn" in variant:
            assert conn.execute("SELECT charged FROM cdn_usage").fetchone()[0] == 123
        if "bot" in variant:
            assert conn.execute("SELECT file_id FROM channel_bot_files").fetchone()[0] == "file-id"
            if variant.startswith("9"):
                assert (
                    conn.execute("SELECT file_id FROM channel_bot_covers").fetchone()[0]
                    == "cover-id"
                )


def test_incompatible_legacy_table_rolls_back_migration_ten():
    with closing(schema_seven()) as conn:
        with conn:
            conn.execute("CREATE TABLE channel_bot_files (item_id TEXT PRIMARY KEY)")
            conn.execute("INSERT INTO channel_bot_files VALUES ('untouched')")
            conn.execute("PRAGMA user_version=9")
        with pytest.raises(RuntimeError, match="channel_bot_files"):
            migrations.migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
        assert conn.execute("SELECT item_id FROM channel_bot_files").fetchone()[0] == "untouched"
        assert (
            conn.execute("SELECT name FROM sqlite_schema WHERE name='cdn_usage'").fetchone() is None
        )
