import pytest
from fastapi import HTTPException

from app.core.ranges import parse_range


def test_full_file():
    assert parse_range(None, 100) == (0, 100, False)


def test_open_ended():
    assert parse_range("bytes=10-", 100) == (10, 100, True)


def test_closed_range():
    assert parse_range("bytes=10-19", 100) == (10, 20, True)


def test_suffix():
    assert parse_range("bytes=-10", 100) == (90, 100, True)


def test_clamp_end():
    assert parse_range("bytes=90-200", 100) == (90, 100, True)


def test_bad_ranges():
    for header in ["bytes=100-", "bytes=5-4", "bytes=1-2,3-4", "nope"]:
        with pytest.raises(HTTPException) as exc:
            parse_range(header, 100)
        assert exc.value.status_code == 416
        assert exc.value.headers["Content-Range"] == "bytes */100"
