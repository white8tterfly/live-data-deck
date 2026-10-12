"""HTTP 请求体模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class TargetIn(BaseModel):
    room_id: int = Field(gt=0)
    vup: str = ""
    enabled: bool = True


class TargetPatch(BaseModel):
    vup: str | None = None
    enabled: bool | None = None


class CookieTestIn(BaseModel):
    sessdata: str | None = None
