from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("tgdrive.uploads")


def faststart_path(path: Path) -> Path:
    return path.with_name(path.stem + ".faststart.part")


def remove_upload_files(path: Path) -> None:
    # Retain the record/original if sidecar deletion fails, so cleanup can retry.
    faststart_path(path).unlink(missing_ok=True)
    path.unlink(missing_ok=True)


def cleanup_faststart_outputs(directory: Path) -> None:
    """At process startup only, before any upload can create an active output."""
    for path in directory.glob("upload_*.faststart.part"):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove leftover faststart output")
