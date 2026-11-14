from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.services.people import MAX_PERSON_LENGTH


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


ItemId = Annotated[str, Field(min_length=1, max_length=64)]
FolderSegment = Annotated[str, Field(min_length=1, max_length=255)]
FolderPath = Annotated[list[FolderSegment], Field(min_length=1, max_length=64)]


class FolderPathsBody(ApiModel):
    model_config = ConfigDict(str_strip_whitespace=False)

    parent_id: ItemId | None = None
    paths: list[FolderPath] = Field(min_length=1, max_length=200)


class BatchItemsBody(ApiModel):
    ids: list[ItemId] = Field(min_length=1, max_length=200)
    action: Literal["move", "trash"]
    parent_id: ItemId | None = None


PeopleLabelName = Annotated[str, Field(min_length=1, max_length=MAX_PERSON_LENGTH)]


class PeopleLabelBody(ApiModel):
    name: PeopleLabelName


class VideoPeopleBody(ApiModel):
    add: list[PeopleLabelName] = Field(default_factory=list, max_length=200)
    remove: list[PeopleLabelName] = Field(default_factory=list, max_length=200)


class ShareBody(ApiModel):
    expires_hours: int = Field(default=0, ge=0, le=24 * 365)


class ShareTokenBody(ApiModel):
    token: str = Field(min_length=1, max_length=100)


class UpdateShareBody(ShareTokenBody):
    expires_hours: int = Field(ge=0, le=24 * 365)


class UploadCreateBody(ApiModel):
    image_host: bool = False
    name: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0)
    mime: str | None = Field(default=None, max_length=255)
    parent_id: str | None = Field(default=None, max_length=64)
