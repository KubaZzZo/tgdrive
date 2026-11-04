from __future__ import annotations

from fastapi import HTTPException


def _invalid(size: int, message: str) -> HTTPException:
    return HTTPException(416, message, headers={"Content-Range": f"bytes */{size}"})


def parse_range(range_header: str | None, size: int) -> tuple[int, int, bool]:
    """Parse a single HTTP byte range.

    Returns (start, end_exclusive, partial). Multi-range requests are
    intentionally rejected; video players only need a single range.
    """
    if not range_header:
        return 0, size, False
    if not range_header.startswith("bytes="):
        raise _invalid(size, "只支持 bytes Range")
    spec = range_header[6:].strip()
    if "," in spec:
        raise _invalid(size, "不支持多段 Range")
    try:
        left, right = spec.split("-", 1)
        if left == "":
            suffix = int(right)
            if suffix <= 0:
                raise ValueError
            start = max(0, size - suffix)
            end = size
        else:
            start = int(left)
            end = int(right) + 1 if right else size
    except ValueError:
        raise _invalid(size, "Range 格式错误") from None
    if start < 0 or start >= size or end <= start:
        raise _invalid(size, "Range 超出文件大小")
    return start, min(end, size), True
