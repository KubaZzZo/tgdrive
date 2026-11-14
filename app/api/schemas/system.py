from pydantic import BaseModel, ConfigDict, Field


class TrafficSettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quota_bytes: int = Field(ge=1, le=10**16)


class SyncBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    full: bool = False
