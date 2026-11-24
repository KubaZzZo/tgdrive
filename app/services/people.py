from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache
from unicodedata import normalize

import jieba.posseg

PERSON_FIELD = re.compile(
    r"^\s*(?:人物|人名|姓名|主演|演员|演員|嘉宾|出镜(?:人|人员)?|"
    r"(?:AV)?(?:女优|女優)|出演(?:者)?|キャスト|cast|starring|actress(?:es)?)\s*:\s*(.*)$",
    re.IGNORECASE,
)
NAME = re.compile(r"[^\W\d_]+(?:[ .·・'’\-]+[^\W\d_]+)*")
MAX_PERSON_LENGTH = 80


def person_key(name: str) -> str:
    return " ".join(normalize("NFKC", name).split()).casefold()


def clean_caption(caption: str) -> str:
    return re.sub(r"https?://\S+|(?<!\w)@[A-Za-z0-9_]+", "", normalize("NFKC", caption), flags=re.I)


def valid_person_name(name: object) -> str:
    if isinstance(name, str):
        clean = " ".join(normalize("NFKC", name).split())
        if 0 < len(clean) <= MAX_PERSON_LENGTH and NAME.fullmatch(clean):
            return clean
    return ""


def _unique_people(candidates: Iterable[str]) -> tuple[str, ...]:
    names: dict[str, str] = {}
    for candidate in candidates:
        if name := valid_person_name(candidate):
            names.setdefault(person_key(name), name)
    return tuple(names.values())


def credited_people(cleaned_caption: str) -> tuple[str, ...] | None:
    """Read names from explicit fields in a normalized caption."""
    fields = [
        match[1] for line in cleaned_caption.splitlines() if (match := PERSON_FIELD.match(line))
    ]
    if not fields:
        return None
    values = [re.split(r"\s+#\w", field, maxsplit=1)[0] for field in fields]
    return _unique_people(re.split(r"[、,;/|&+]", ",".join(values)))


@lru_cache(maxsize=256)
def extract_people(caption: str) -> tuple[str, ...]:
    """Read explicit credits first, otherwise use local Chinese POS tagging."""
    caption = clean_caption(caption)
    credits = credited_people(caption)
    if credits is not None:
        return credits
    if not re.search(r"[\u3400-\u9fff]", caption):
        return ()
    return _unique_people(
        word for word, tag in jieba.posseg.cut(caption) if tag in {"nr", "nrj", "nrf"}
    )
