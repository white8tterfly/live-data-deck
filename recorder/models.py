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
        if value < 0:
            return
        self.popularity_now = value
        if value > self.popularity_peak:
            self.popularity_peak = value

    def uptime_sec(self) -> int:
        return int(time.time() - self.started_at) if self.started_at else 0

    def conn_sec(self) -> int:
        return int(time.time() - self.connected_at) if self.connected_at else 0

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
            "counters": dict(self.counters),
        }
