"""房间运行时状态模型（录制协程写、管理器读、API 序列化）。"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

# 0=未开播 1=直播中 2=轮播
STATUS_TEXT = {0: "未开播", 1: "直播中", 2: "轮播"}


@dataclass
class RoomRuntime:
    room_id: int
    vup: str
    enabled: bool = True

    # 任务/连接状态
    running: bool = False          # 录制协程是否存活
    connected: bool = False        # WS 是否在线
    live_status: int = 0           # 最近一次开播状态
    anchor_name: str = ""          # 主播昵称（Room/get_info）
    title: str = ""                # 直播标题
    current_file: str = ""         # 当日落盘文件名
    host_index: int = 0            # 当前节点序号（从 1 展示）
    host_count: int = 0
    retries: int = 0               # 重连次数
    last_error: str = ""

    started_at: float = 0          # 任务启动时间
    connected_at: float = 0        # 本次 WS 连接时间
    last_event_at: float = 0       # 最近一条弹幕/事件时间

    # 人气值（WS op=3 心跳应答，25s 粒度；平台加权指标，非独立人数）
    popularity_now: int = 0
    popularity_peak: int = 0

    # 视频录制（附加功能；record_video 为该房间的独立开关，可在运行中动态切换）
    record_video: bool = False
    notify: bool = False               # 开播提醒订阅（浏览器通知/邮件等通道共用）
    # 房间级配置覆盖（None=跟随全局默认；与 config.Target 同名，ov_ 前缀避免与运行态混淆）
    ov_record_sc: bool | None = None
    ov_record_gifts: bool | None = None
    ov_record_moderation: bool | None = None
    ov_filter_pure_emoticon: bool | None = None
    ov_anonymize: bool | None = None
    ov_sanitize_text: bool | None = None
    ov_video_qn: int | None = None
    ov_llm_auto_analyze: bool | None = None
    video_active: bool = False        # 视频流是否正在落盘
    video_session: str = ""           # 场次文件名前缀 live_YYYYMMDD_HHMM
    video_file: str = ""              # 当前分段文件名
    video_bytes: int = 0              # 本场累计字节
    video_segments: int = 0           # 已写分段数（含当前）
    video_started_at: float = 0       # 本场起录 unix 时间
    video_qn: int = 0                 # 实际下发画质（qn）
    video_error: str = ""

    counters: dict = field(default_factory=lambda: {
        "danmaku": 0, "super_chat": 0, "gift": 0,
        "emoticon_skipped": 0, "moderation": 0,
    })
    _rate_window: deque = field(default_factory=lambda: deque())

    def bump(self, kind: str, n: int = 1) -> None:
        self.counters[kind] = self.counters.get(kind, 0) + n
        self.last_event_at = time.time()
        if kind in ("danmaku", "super_chat", "gift"):
            self._rate_window.append(self.last_event_at)
            self._trim_rate()

    def _trim_rate(self) -> None:
        cutoff = time.time() - 60
        w = self._rate_window
        while w and w[0] < cutoff:
            w.popleft()

    @property
    def rate_per_min(self) -> int:
        self._trim_rate()
        return len(self._rate_window)

    def update_popularity(self, value: int) -> None:
        # <=1 视为无效：WS 心跳在现网固定回 1，HTTP 失败兜底也可能给 0
        if value <= 1:
            return
        self.popularity_now = value
        if value > self.popularity_peak:
            self.popularity_peak = value

    def uptime_sec(self) -> int:
        return int(time.time() - self.started_at) if self.started_at else 0

    def conn_sec(self) -> int:
        return int(time.time() - self.connected_at) if self.connected_at else 0

    def video_reset(self) -> None:
        """一场视频录制结束后清空运行态（meta/ass 已落盘）。"""
        self.video_active = False
        self.video_session = ""
        self.video_file = ""
        self.video_bytes = 0
        self.video_segments = 0
        self.video_started_at = 0
        self.video_qn = 0
        self.video_error = ""

    def video_sec(self) -> int:
        return int(time.time() - self.video_started_at) if self.video_started_at else 0

    def phase(self) -> str:
        """供前端展示的聚合相位。"""
        if not self.running:
            return "stopped"
        if self.connected:
            if self.live_status == 1:
                return "recording"
            return "connected_idle"
        if self.live_status == 1:
            return "connecting"
        return "waiting"

    def snapshot(self) -> dict:
        return {
            "room_id": self.room_id,
            "vup": self.vup,
            "enabled": self.enabled,
            "running": self.running,
            "connected": self.connected,
            "phase": self.phase(),
            "live_status": self.live_status,
            "live_status_text": STATUS_TEXT.get(self.live_status, "未知"),
            "anchor_name": self.anchor_name,
            "title": self.title,
            "current_file": self.current_file,
            "host_index": self.host_index,
            "host_count": self.host_count,
            "retries": self.retries,
            "last_error": self.last_error,
            "started_at": self.started_at,
            "connected_at": self.connected_at,
            "last_event_at": self.last_event_at,
            "uptime_sec": self.uptime_sec(),
            "conn_sec": self.conn_sec(),
            "rate_per_min": self.rate_per_min,
            "popularity_now": self.popularity_now,
            "popularity_peak": self.popularity_peak,
            "record_video": self.record_video,
            "notify": self.notify,
            "video_active": self.video_active,
            "video_session": self.video_session,
            "video_file": self.video_file,
            "video_bytes": self.video_bytes,
            "video_segments": self.video_segments,
            "video_started_at": self.video_started_at,
            "video_sec": self.video_sec(),
            "video_qn": self.video_qn,
            "video_error": self.video_error,
            "counters": dict(self.counters),
            "overrides": {
                "record_sc": self.ov_record_sc,
                "record_gifts": self.ov_record_gifts,
                "record_moderation": self.ov_record_moderation,
                "filter_pure_emoticon": self.ov_filter_pure_emoticon,
                "anonymize": self.ov_anonymize,
                "sanitize_text": self.ov_sanitize_text,
                "video_qn": self.ov_video_qn,
                "llm_auto_analyze": self.ov_llm_auto_analyze,
            },
        }
