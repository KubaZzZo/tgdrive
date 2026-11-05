from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LoginBody(ApiModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    password: str = Field(min_length=1, max_length=512)


class FolderBody(ApiModel):
    name: str = Field(min_length=1, max_length=255)
    parent_id: str | None = Field(default=None, max_length=64)


class UpdateItemBody(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    parent_id: str | None = Field(default=None, max_length=64)
    move: bool = False


class ShareBody(ApiModel):
    expires_hours: int = Field(default=0, ge=0, le=24 * 365)


class RevokeShareBody(ApiModel):
    token: str = Field(min_length=1, max_length=128)


class UploadCreateBody(ApiModel):
    name: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0)
    mime: str | None = Field(default=None, max_length=255)
    parent_id: str | None = Field(default=None, max_length=64)
