from __future__ import annotations

import re
from functools import lru_cache
from unicodedata import normalize

import jieba.posseg

PERSON_FIELD = re.compile(r"^\s*(?:人物|人名|姓名|主演|演员|嘉宾|出镜(?:人|人员)?)\s*:\s*(.*)$")
NAME = re.compile(r"[^\W\d_]+(?:[ .·・'’\-]+[^\W\d_]+)*")
MAX_PERSON_LENGTH = 80


def person_key(name: str) -> str:
    return " ".join(normalize("NFKC", name).split()).casefold()


@lru_cache(maxsize=256)
def extract_people(caption: str) -> tuple[str, ...]:
    """Read explicit credits first, otherwise use local Chinese POS tagging."""
    caption = normalize("NFKC", caption)
    caption = re.sub(r"https?://\S+|(?<!\w)@[A-Za-z0-9_]+", "", caption, flags=re.I)
    fields = [match[1] for line in caption.splitlines() if (match := PERSON_FIELD.match(line))]
    if fields:
        candidates = re.split(r"[、,;/|&]", ",".join(fields))
    elif re.search(r"[\u3400-\u9fff]", caption):
        candidates = [
            word for word, tag in jieba.posseg.cut(caption) if tag in {"nr", "nrj", "nrf"}
        ]
    else:
        return ()

    names: dict[str, str] = {}
    for candidate in candidates:
        name = " ".join(candidate.split())
        if 0 < len(name) <= MAX_PERSON_LENGTH and NAME.fullmatch(name):
            names.setdefault(person_key(name), name)
    return tuple(names.values())
