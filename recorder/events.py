"""异步事件总线：录制协程产生事件，Web 端 WebSocket / 日志缓冲消费。

- emit() 可在同步/异步上下文中调用（put_nowait，绝不阻塞录制循环）
- 每个订阅者独立队列；慢消费者丢老消息，不影响其它订阅者
- 最近 LOG_KEEP 条日志环形留存，供新打开的页面补发
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

LOG_KEEP = 500
LOG_LEVELS = ("debug", "info", "warning", "error")


class EventBus:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._subscribers: set[asyncio.Queue] = set()
        self._pump_task: asyncio.Task | None = None
        self.log_buffer: deque[dict] = deque(maxlen=LOG_KEEP)

    async def start(self) -> None:
        self._queue = asyncio.Queue(maxsize=10000)
        self._pump_task = asyncio.create_task(self._pump())

    async def stop(self) -> None:
        if self._pump_task:
            self._pump_task.cancel()
            self._pump_task = None
        for q in list(self._subscribers):
            try:
                q.put_nowait({"type": "shutdown", "ts": time.time(), "data": {}})
            except Exception:
                pass

    def emit(self, type_: str, data: dict[str, Any] | None = None) -> None:
        if self._queue is None:
            return
        evt = {"type": type_, "ts": time.time(), "data": data or {}}
        try:
            self._queue.put_nowait(evt)
        except asyncio.QueueFull:
            try:  # 极端峰值：丢一条最老事件腾位
                self._queue.get_nowait()
                self._queue.put_nowait(evt)
            except Exception:
                pass

    def log(self, level: str, msg: str, room_id: int | None = None) -> None:
        level = level if level in LOG_LEVELS else "info"
        rec = {"level": level, "msg": msg, "room_id": room_id,
               "ts": time.time()}
        self.log_buffer.append(rec)
        self.emit("log", rec)

    def recent_logs(self, limit: int = 300) -> list[dict]:
        if limit <= 0:
            return list(self.log_buffer)
        return list(self.log_buffer)[-limit:]

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    async def _pump(self) -> None:
        assert self._queue is not None
        while True:
            evt = await self._queue.get()
            dead: list[asyncio.Queue] = []
            for q in self._subscribers:
                try:
                    q.put_nowait(evt)
                except asyncio.QueueFull:
                    try:  # 慢消费者：丢最老事件
                        q.get_nowait()
                        q.put_nowait(evt)
                    except Exception:
                        dead.append(q)
            for q in dead:
                self._subscribers.discard(q)
