from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class VideoError(ValueError):
    pass


class VideoSource(BaseModel):
    url: str = ""
    path: Path | None = None
    file_id: str = ""
    file_uuid: str = ""
    name: str = "视频"


class VideoResult(BaseModel):
    summary: str
    duration_seconds: float | None = None
    frame_timestamps: list[float] = Field(default_factory=list)
    audio_transcribed: bool = False
    mode: str = "frames"
    warnings: list[str] = Field(default_factory=list)


class Segment(BaseModel):
    type: str
    data: dict[str, Any] = Field(default_factory=dict)


class MessagePayload(BaseModel):
    message: list[Segment] | str = Field(default_factory=list)
    group_id: int | None = None
    message_type: str = ""
    user_id: int | None = None
    sender: dict[str, Any] = Field(default_factory=dict)
    records: list[dict[str, Any]] = Field(default_factory=list)


def response_data(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise VideoError("平台返回了无效的数据结构")
    data = raw.get("data", raw)
    if not isinstance(data, dict):
        raise VideoError("平台响应中缺少数据")
    return data
