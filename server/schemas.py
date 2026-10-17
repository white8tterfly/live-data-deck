"""HTTP 请求体模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class TargetIn(BaseModel):
    """新增单个房间的请求体。"""
    room_id: int = Field(gt=0)
    vup: str = ""
    enabled: bool = True
    record_video: bool = False
    notify: bool = False


class TargetPatch(BaseModel):
    """局部更新房间：字段为 None 表示不改动。"""
    vup: str | None = None
    enabled: bool | None = None
    record_video: bool | None = None
    notify: bool | None = None
    # 房间级覆盖：key 见 config.ROOM_BOOL_OVERRIDES / ROOM_INT_OVERRIDES；
    # 值显式为 null 表示恢复「跟随全局」。校验在 config.coerce_room_overrides。
    overrides: dict | None = None


class TargetBulkItem(BaseModel):
    """批量导入里的单个房间。"""
    room_id: int = Field(gt=0)
    vup: str = ""


class TargetsBulkIn(BaseModel):
    """从关注列表批量导入的请求体。"""
    items: list[TargetBulkItem] = Field(min_length=1, max_length=300)
    enabled: bool = True


class CookieTestIn(BaseModel):
    """测试 SESSDATA 的请求体；不填就当用已保存的。"""
    sessdata: str | None = None


class CaredUserIn(BaseModel):
    """新增关心用户的请求体。"""
    key: str = Field(min_length=1)
    note: str = ""


class AnalysisIn(BaseModel):
    """分析请求体，给出要分析的 jsonl 相对路径。"""
    paths: list[str] = Field(min_length=1, max_length=30)
    kind: str = "records"


class DedupIn(BaseModel):
    """单文件去重请求体。"""
    path: str
    kind: str = "records"
    window_sec: int = Field(default=300, ge=0, le=86400)


class DedupBatchIn(BaseModel):
    """批量去重请求体。"""
    paths: list[str] = Field(min_length=1, max_length=30)
    kind: str = "records"
    window_sec: int = Field(default=300, ge=0, le=86400)


class ReportIn(BaseModel):
    """战报请求体，摘要与出图共用。"""
    path: str
    kind: str = "records"


class VideoCutIn(BaseModel):
    """视频切片请求体。"""
    vup: str = Field(min_length=1, max_length=120)
    session: str = Field(min_length=1, max_length=80)   # meta 文件名前缀 base
    part: str = Field(min_length=1, max_length=120)
    start_sec: float = Field(ge=0, le=86400)
    end_sec: float = Field(ge=0, le=86400)


class VideoSessionIn(BaseModel):
    """用 vup + session（meta 文件名前缀 base）定位一场的请求体。"""
    vup: str = Field(min_length=1, max_length=120)
    session: str = Field(min_length=1, max_length=80)


class EcgSeekIn(BaseModel):
    """心电图点击 seek 的请求体。"""
    vup: str = Field(min_length=1, max_length=120)
    session: str = Field(min_length=1, max_length=80)
    ts: float = Field(ge=0)


class VideoAiIn(BaseModel):
    """触发 AI 高能解读的请求体。"""
    vup: str = Field(min_length=1, max_length=120)
    session: str = Field(min_length=1, max_length=80)
    mode: str | None = None        # A / C；缺省用全局设置 llm_mode
    force: bool = False            # 重新分析（忽略 ai.json 缓存）
    window_sec: int = Field(default=60, ge=10, le=600)
    top_n: int = Field(default=8, ge=1, le=20)


class LlmTestIn(BaseModel):
    """LLM 连通性测试请求体。"""
    base_url: str = Field(min_length=1, max_length=300)
    api_key: str | None = None     # 留空则用已保存的 Key
    model: str = Field(min_length=1, max_length=120)
