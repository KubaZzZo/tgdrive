from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from app.infrastructure.db import Database
from app.infrastructure.db.common import utcnow

INDEX_OBJECTS = {
    "items",
    "file_parts",
    "shares",
    "image_hosts",
    "channel_messages",
    "channel_sync_state",
    "channel_bot_files",
    "channel_bot_covers",
    "video_people",
    "people_labels",
    "video_people_edits",
    "effective_video_people",
}


def build_index_backup(db: Database, directory: Path, chat_id: str, app_version: str) -> Path:
    index_path = directory / "index.sqlite3"
    with closing(sqlite3.connect(index_path)) as snapshot:
        db.backup_to(snapshot)
        schema_version = int(snapshot.execute("PRAGMA user_version").fetchone()[0])
        snapshot.execute("PRAGMA journal_mode=DELETE")
        with snapshot:
            objects = snapshot.execute(
                "SELECT type,name FROM sqlite_schema "
                "WHERE type IN ('table','view','trigger') AND substr(name,1,7)!='sqlite_' "
                "ORDER BY CASE type WHEN 'trigger' THEN 0 WHEN 'view' THEN 1 ELSE 2 END"
            ).fetchall()
            for kind, name in objects:
                if name not in INDEX_OBJECTS:
                    escaped = name.replace('"', '""')
                    snapshot.execute(f'DROP {kind} "{escaped}"')
        # Rebuild the SQLite file so discarded tables leave no data in free pages.
        snapshot.execute("VACUUM")

    manifest = {
        "format": "tgdrive-channel-index",
        "format_version": 1,
        "channel_id": chat_id,
        "app_version": app_version,
        "source_schema_version": schema_version,
        "created_at": utcnow(),
    }
    archive = directory / "index.zip"
    with ZipFile(archive, "w", ZIP_DEFLATED) as backup:
        backup.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        backup.write(index_path, "index.sqlite3")
    return archive
