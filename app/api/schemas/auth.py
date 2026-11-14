from __future__ import annotations

from pydantic import ConfigDict, Field

from app.api.schemas.base import ApiModel


class LoginBody(ApiModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    password: str = Field(min_length=1, max_length=512)
