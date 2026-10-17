"""异步事件总线：录制协程产生事件，Web 端 WebSocket / 日志缓冲消费。

- emit() 可在同步/异步上下文中调用（put_nowait，绝不阻塞录制循环）
- 每个订阅者独立队列；慢消费者丢老消息，不影响其它订阅者
- 最近 LOG_KEEP 条日志环形留存，供新打开的页面补发
- 日志同时按天滚动落盘（app.log，保留 LOG_KEEP_DAYS 天），由独立线程写盘
"""
from __future__ import annotations

import asyncio
import logging
import queue
import time
from collections import deque
from pathlib import Path
from logging.handlers import QueueHandler, QueueListener, TimedRotatingFileHandler
from typing import Any

LOG_KEEP = 500
LOG_KEEP_DAYS = 14
LOG_LEVELS = ("debug", "info", "warning", "error")
_LOG_LEVEL_NO = {"debug": logging.DEBUG, "info": logging.INFO,
                 "warning": logging.WARNING, "error": logging.ERROR}
_LOG_FILE_NAME = "app.log"


class EventBus:
    """进程内异步事件总线：录制侧 emit，WebSocket 订阅者与日志缓冲消费。"""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._subscribers: set[asyncio.Queue] = set()
        self._pump_task: asyncio.Task | None = None
        self.log_buffer: deque[dict] = deque(maxlen=LOG_KEEP)
        # 文件日志（后台线程写盘，绝不阻塞事件循环）
        self._file_dir: Path | None = None
        self._file_queue: "queue.Queue | None" = None
        self._file_listener: QueueListener | None = None
        self._file_handler: TimedRotatingFileHandler | None = None
        self._file_logger: logging.Logger | None = None

    async def start(self) -> None:
        """建事件队列并启动分发协程（须在事件循环内调用）。"""
        self._queue = asyncio.Queue(maxsize=10000)
        self._pump_task = asyncio.create_task(self._pump())

    async def stop(self) -> None:
        """停分发协程、给订阅者发 shutdown，并关闭文件日志。"""
        if self._pump_task:
            self._pump_task.cancel()
            self._pump_task = None
        for q in list(self._subscribers):
            try:
                q.put_nowait({"type": "shutdown", "ts": time.time(), "data": {}})
            except Exception:
                pass
        self.close_log_file()

    # 文件日志
    def configure_log_file(self, log_dir: Path) -> Path:
        """启用/切换按天滚动的文件日志。目录不存在则创建；同目录重复调用幂等。

        写盘经 QueueHandler 丢给独立线程，调用方（事件循环）只做一次非阻塞入队。
        """
        log_dir = Path(log_dir)
        if self._file_dir is not None and self._file_dir == log_dir.resolve():
            return self._file_dir
        self.close_log_file()
        log_dir.mkdir(parents=True, exist_ok=True)
        q: queue.Queue = queue.Queue(-1)
        handler = TimedRotatingFileHandler(
            log_dir / _LOG_FILE_NAME, when="midnight",
            backupCount=LOG_KEEP_DAYS, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"))
        listener = QueueListener(q, handler, respect_handler_level=True)
        listener.start()
        lg = logging.getLogger("ldd.eventbus.file")
        lg.setLevel(logging.DEBUG)
        lg.propagate = False  # 不冒泡到 root，避免在控制台重复打印
        lg.addHandler(QueueHandler(q))
        self._file_dir = log_dir.resolve()
        self._file_queue = q
        self._file_listener = listener
        self._file_handler = handler
        self._file_logger = lg
        return self._file_dir

    def close_log_file(self) -> None:
        """停止后台写盘线程并关闭句柄（设置中切换目录 / 服务关闭时调用）。"""
        if self._file_logger is not None:
            for h in list(self._file_logger.handlers):
                self._file_logger.removeHandler(h)
                try:
                    h.close()
                except Exception:
                    pass
            self._file_logger = None
        if self._file_listener is not None:
            try:
                self._file_listener.stop()
            except Exception:
                pass
            self._file_listener = None
        if self._file_handler is not None:
            try:
                self._file_handler.close()
            except Exception:
                pass
            self._file_handler = None
        self._file_queue = None
        self._file_dir = None

    @property
    def log_file_dir(self) -> Path | None:
        """当前文件日志目录；未启用返回 None。"""
        return self._file_dir

    def emit(self, type_: str, data: dict[str, Any] | None = None) -> None:
        """投递事件到主队列（非阻塞）。未 start 时静默丢弃，队列满时丢一条最老事件腾位。"""
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
        """写一条运行日志：进环形缓冲、落盘文件，并投递给前端。

        level 不在白名单内时按 info 处理；文件日志失败只吞掉异常，不影响录制与页面。
        """
        level = level if level in LOG_LEVELS else "info"
        rec = {"level": level, "msg": msg, "room_id": room_id,
               "ts": time.time()}
        self.log_buffer.append(rec)
        if self._file_logger is not None:
            try:
                text = f"[room {room_id}] {msg}" if room_id else msg
                self._file_logger.log(_LOG_LEVEL_NO[level], "%s", text)
            except Exception:
                pass  # 文件日志失败绝不影响录制与页面日志
        self.emit("log", rec)

    def recent_logs(self, limit: int = 300) -> list[dict]:
        """取最近 limit 条日志；limit<=0 时返回全部缓冲。"""
        if limit <= 0:
            return list(self.log_buffer)
        return list(self.log_buffer)[-limit:]

    def subscribe(self) -> asyncio.Queue:
        """登记新订阅者并返回其专属队列（各队列独立，互不拖累）。"""
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        """摘掉订阅者队列。"""
        self._subscribers.discard(q)

    async def _pump(self) -> None:
        """主队列分发循环：扇出到各订阅者，队列满的慢消费者丢最老消息、多次失败则剔除。"""
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
