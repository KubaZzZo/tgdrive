"""Server-owned classification shared by every upload client."""

import mimetypes
from pathlib import PurePath
from typing import Literal

Category = Literal["image", "video", "audio", "document", "archive", "code", "application", "other"]
CATEGORY_LABELS = {
    "image": "图片",
    "video": "视频",
    "audio": "音频",
    "document": "文档",
    "archive": "压缩包",
    "code": "代码",
    "application": "安装包",
    "other": "其他",
}
EXTENSIONS = {
    "image": "jpg jpeg png gif webp bmp svg ico tif tiff heic heif avif raw dng cr2 nef",
    "video": "mp4 mov m4v webm mkv avi wmv flv mpg mpeg ts mts m2ts 3gp",
    "audio": "mp3 m4a aac flac wav ogg opus wma aiff alac amr mid midi",
    "document": "pdf doc docx xls xlsx ppt pptx odt ods odp rtf txt md csv tsv epub mobi azw azw3 pages numbers key",
    "archive": "zip rar 7z tar gz bz2 xz tgz zst cab iso",
    "code": "py js ts jsx tsx vue svelte html css scss less json yaml yml xml sql sh ps1 bat c cpp h hpp rs go java kt swift rb php ipynb toml ini conf log",
    "application": "exe msi msix apk apks xapk ipa dmg pkg deb rpm appimage",
}
BY_EXTENSION = {
    ext: category for category, extensions in EXTENSIONS.items() for ext in extensions.split()
}


def classify_file(name: str, mime: str | None) -> str:
    # Extensions resolve common mobile/generic MIME values, and docx reported as zip.
    extension = PurePath(name.lower()).suffix.lstrip(".")
    if extension in BY_EXTENSION:
        return BY_EXTENSION[extension]
    media_type = (mime or "").split(";", 1)[0].strip().lower()
    for prefix in ("image", "video", "audio"):
        if media_type.startswith(prefix + "/"):
            return prefix
    if (
        media_type.startswith("text/")
        or media_type
        in {
            "application/pdf",
            "application/msword",
            "application/epub+zip",
        }
        or media_type.startswith(
            (
                "application/vnd.openxmlformats",
                "application/vnd.oasis.opendocument",
                "application/vnd.ms-",
            )
        )
    ):
        return "document"
    if media_type in {
        "application/zip",
        "application/x-rar-compressed",
        "application/x-7z-compressed",
        "application/gzip",
        "application/x-tar",
    }:
        return "archive"
    if media_type in {"application/json", "application/javascript", "application/xml"}:
        return "code"
    if media_type in {
        "application/vnd.android.package-archive",
        "application/x-msdownload",
        "application/x-apple-diskimage",
    }:
        return "application"
    return "other"


def effective_mime(name: str, mime: str | None) -> str:
    declared = (mime or "").strip()
    if declared.lower() not in {"", "application/octet-stream", "binary/octet-stream"}:
        return declared
    return mimetypes.guess_type(name)[0] or "application/octet-stream"
