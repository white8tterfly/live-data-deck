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


class CaredUserIn(BaseModel):
    key: str = Field(min_length=1)
    note: str = ""


class AnalysisIn(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=30)
    kind: str = "records"


class DedupIn(BaseModel):
    path: str
    kind: str = "records"
    window_sec: int = Field(default=300, ge=0, le=86400)


class DedupBatchIn(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=30)
    kind: str = "records"
    window_sec: int = Field(default=300, ge=0, le=86400)


class ReportIn(BaseModel):
    path: str
    kind: str = "records"
