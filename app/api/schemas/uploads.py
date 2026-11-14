from __future__ import annotations

from pydantic import Field

from app.api.schemas.base import ApiModel


class UploadCreateBody(ApiModel):
    image_host: bool = False
    name: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0)
    mime: str | None = Field(default=None, max_length=255)
    parent_id: str | None = Field(default=None, max_length=64)
