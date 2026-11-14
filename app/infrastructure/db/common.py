from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def clean_name(name: str, *, preserve_spaces: bool = False) -> str:
    trimmed = (name or "").strip()
    if not trimmed:
        raise ValueError("名称不能为空")
    name = name if preserve_spaces else trimmed
    if len(name) > 255:
        raise ValueError("名称过长")
    if "/" in name or "\\" in name or trimmed in {".", ".."}:
        raise ValueError("名称包含非法字符")
    return name
