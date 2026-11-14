from pydantic import BaseModel, ConfigDict, Field


class PasswordBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=1, max_length=512)


class ChannelBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    chat_id: str = Field(min_length=1, max_length=128)
