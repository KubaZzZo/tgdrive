"""Public image formats are restricted to inert raster content."""

MAX_IMAGE_SIZE = 20 * 1024 * 1024
IMAGE_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


def image_mime(header: bytes) -> str:
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("图床仅支持 JPEG、PNG、GIF、WebP 图片，请检查文件内容")
