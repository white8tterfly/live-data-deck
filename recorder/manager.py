"""多房间录制任务管理器。

- 动态增删房间任务（无需重启进程，区别于旧版一次性脚本）
- 聚合运行时状态，每 5s 广播一次快照给 Web 端
- 全部关播 N 分钟自动停止（idle watchdog，60s 轮询，查询失败保守视为在播）
- SESSDATA 更新后自动滚动重启运行中的房间任务
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import httpx

from . import protocol as P
from .config import Settings, SettingsStore, Target, TargetStore
from .events import EventBus
from .models import RoomRuntime
from .room_task import record_room
from .theme import ThemeStore

SNAPSHOT_INTERVAL = 5
START_STAGGER_SEC = 2.5     # 错峰启动，避免多房间同秒握手触发账号级风控
WATCHDOG_POLL_SEC = 60


class RecorderManager:
    def __init__(self) -> None:
        self.bus = EventBus()
        self.settings_store = SettingsStore()
        self.target_store = TargetStore()
        self.theme_store = ThemeStore()
        self.settings: Settings = self.settings_store.settings
        self.tasks: dict[int, asyncio.Task] = {}
        self.runtimes: dict[int, RoomRuntime] = {}
        self._snapshot_task: asyncio.Task | None = None
        self._idle_task: asyncio.Task | None = None
        self._bg_stagger: set[asyncio.Task] = set()
        self.started_at = 0.0

    # ---------- 生命周期 ----------
    async def start(self) -> None:
        self.started_at = time.time()
        await self.bus.start()
        try:
            self.bus.configure_log_file(self.settings.log_path())
        except OSError as e:
            # 日志落盘失败不阻断服务（页面内存日志仍可用）
            print(f"[manager] 日志目录初始化失败：{e}", flush=True)
        for t in self.target_store.targets:
            self.runtimes[t.room_id] = self._make_rt(t)
        self._snapshot_task = asyncio.create_task(self._snapshot_loop())
        if self.settings.auto_start and self.target_store.targets:
            asyncio.create_task(self._start_all())
            self.bus.log("info", "服务启动，自动开始录制已启用的房间")

    async def shutdown(self) -> None:
        await self.stop_all()
        if self._snapshot_task:
            self._snapshot_task.cancel()
        await self.bus.stop()

    def _make_rt(self, t: Target) -> RoomRuntime:
        rt = RoomRuntime(room_id=t.room_id, vup=t.vup, enabled=t.enabled)
        rt.record_video = t.record_video
        rt.notify = t.notify
        self._apply_overrides(rt, t)
        return rt

    @staticmethod
    def _apply_overrides(rt: RoomRuntime, t: Target) -> None:
        """把 Target 的房间级覆盖项同步到运行时（None=跟随全局）。"""
        rt.ov_record_sc = t.record_sc
        rt.ov_record_gifts = t.record_gifts
        rt.ov_record_moderation = t.record_moderation
        rt.ov_filter_pure_emoticon = t.filter_pure_emoticon
        rt.ov_anonymize = t.anonymize
        rt.ov_sanitize_text = t.sanitize_text
        rt.ov_video_qn = t.video_qn
        rt.ov_llm_auto_analyze = t.llm_auto_analyze

    async def _snapshot_loop(self) -> None:
        while True:
            await asyncio.sleep(SNAPSHOT_INTERVAL)
            self.bus.emit("state", self.snapshot())

    # ---------- 房间 CRUD ----------
    async def add_target(self, room_id: int, vup: str,
                         enabled: bool = True, persist: bool = True,
                         autostart: bool = True,
                         record_video: bool = False,
                         notify: bool = False) -> Target:
        if room_id in self.runtimes:
            raise ValueError(f"房间 {room_id} 已存在")
        if persist:
            t = self.target_store.add(room_id, vup, enabled,
                                      record_video=record_video,
                                      notify=notify)
        else:
            t = Target(room_id=room_id, vup=vup or f"room_{room_id}",
                       enabled=enabled, record_video=record_video,
                       notify=notify)
        self.runtimes[room_id] = self._make_rt(t)
        self.bus.log("info", f"已添加房间 {room_id}（{t.vup}）")
        if autostart and t.enabled:
            await self.start_room(room_id)
        return t

    async def add_targets_bulk(
        self, pairs: list[tuple[int, str]], enabled: bool = True
    ) -> tuple[list[Target], list[int], list[int]]:
        """批量导入房间并错峰启动（关注列表一键导入用）。

        返回 (新增目标, 跳过的 room_id, 实际启动的 room_id)。
        """
        new, skipped = self.target_store.bulk_add(pairs, enabled)
        for t in new:
            self.runtimes[t.room_id] = self._make_rt(t)
        if new:
            self.bus.log("info",
                         f"批量导入 {len(new)} 个房间"
                         f"（跳过已存在 {len(skipped)} 个）")
        # 错峰启动放到后台：一次导入几十上百间时，不能让 HTTP 请求阻塞数分钟
        todo = [t.room_id for t in new if enabled and t.enabled]
        if todo:
            task = asyncio.create_task(self._stagger_start(todo))
            self._bg_stagger.add(task)
            task.add_done_callback(self._bg_stagger.discard)
        self.bus.emit("state", self.snapshot())
        return new, skipped, []

    async def _stagger_start(self, room_ids: list[int]) -> None:
        """后台按 START_STAGGER_SEC 间隔逐个启动导入的房间。"""
        for i, rid in enumerate(room_ids):
            if i:
                await asyncio.sleep(START_STAGGER_SEC)
            try:
                if not self.is_running(rid):
                    await self.start_room(rid)
            except Exception as e:
                self.bus.log("error", f"房间 {rid} 启动失败：{e}")

    async def update_target(self, room_id: int, vup: str | None = None,
                            enabled: bool | None = None,
                            record_video: bool | None = None,
                            notify: bool | None = None,
                            overrides: dict | None = None) -> Target:
        t = self.target_store.update(room_id, vup, enabled,
                                     record_video=record_video,
                                     notify=notify, overrides=overrides)
        rt = self.runtimes.get(room_id)
        if rt:
            rt.vup = t.vup
            rt.enabled = t.enabled
            # 房间级视频开关：运行中动态生效，正在直播最迟 2s 起录
            if record_video is not None:
                rt.record_video = t.record_video
                if not t.record_video:
                    self.bus.log(
                        "info",
                        f"房间 {rt.vup} 视频录制已关闭"
                        f"（正在录制的场次结束后停止，之后不再自动起录）",
                        room_id)
                elif not self.settings.video_enabled:
                    self.bus.log(
                        "info",
                        f"房间 {rt.vup} 已勾选视频录制；请在设置页开启全局视频开关"
                        f"（开启后正在直播立即起录，未开播则开播自动起录）",
                        room_id)
                else:
                    self.bus.log(
                        "info",
                        f"房间 {rt.vup} 视频录制已开启"
                        f"（正在直播立即起录，未开播则开播自动起录）",
                        room_id)
            if notify is not None:
                rt.notify = t.notify
            if overrides is not None:
                # 弹幕事件类覆盖由 room_task 每条消息实时读取，立即生效；
                # 画质在下次取流/重连生效；AI 自动解读在下一场结束判定时生效
                self._apply_overrides(rt, t)
                self.bus.log("info",
                             f"房间 {rt.vup} 的房间级录制配置已更新"
                             f"（事件开关立即生效，画质下次取流生效）", room_id)
        self.bus.emit("room_state", rt.snapshot() if rt else {})
        return t

    async def remove_target(self, room_id: int) -> None:
        await self.stop_room(room_id)
        self.target_store.remove(room_id)
        self.runtimes.pop(room_id, None)
        self.bus.log("info", f"已删除房间 {room_id}")

    # ---------- 录制控制 ----------
    def is_running(self, room_id: int) -> bool:
        tk = self.tasks.get(room_id)
        return bool(tk and not tk.done())

    async def start_room(self, room_id: int) -> None:
        rt = self.runtimes.get(room_id)
        if not rt:
            raise KeyError(f"房间 {room_id} 不存在")
        if self.is_running(room_id):
            return
        rt.enabled = True
        # 每个任务持有设置快照：任务运行期间一般配置不受后续变更影响；
        # 但视频全局开关通过 provider 实时读取，设置页切换即时生效
        snap = replace(self.settings)
        task = asyncio.create_task(
            record_room(rt, snap, self.bus,
                        video_enabled=lambda: self.settings.video_enabled),
            name=f"record-{room_id}")
        self.tasks[room_id] = task
        self.bus.emit("room_state", rt.snapshot())

        def _on_done(t: asyncio.Task) -> None:
            if t.cancelled():
                return
            if t.exception():
                self.bus.log("error",
                             f"[{rt.vup}] 录制任务异常退出: "
                             f"{type(t.exception()).__name__}: {t.exception()}",
                             room_id)

        task.add_done_callback(_on_done)

    async def stop_room(self, room_id: int) -> None:
        task = self.tasks.pop(room_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _start_all(self) -> list[int]:
        targets = [t for t in self.target_store.targets if t.enabled]
        started: list[int] = []
        for i, t in enumerate(targets):
            if i:
                await asyncio.sleep(START_STAGGER_SEC)
            if not self.is_running(t.room_id):
                await self.start_room(t.room_id)
                started.append(t.room_id)
        # idle watchdog
        if self.settings.idle_stop_minutes > 0:
            if self._idle_task and not self._idle_task.done():
                self._idle_task.cancel()
            self._idle_task = asyncio.create_task(self._idle_watchdog())
        self.bus.emit("state", self.snapshot())
        return started

    async def start_all(self) -> list[int]:
        self.bus.log("info", "开始录制全部已启用房间")
        return await self._start_all()

    async def stop_all(self) -> None:
        if self._idle_task:
            self._idle_task.cancel()
            self._idle_task = None
        ids = list(self.tasks.keys())
        for rid in ids:
            await self.stop_room(rid)
        self.bus.log("info", "全部录制任务已停止")
        self.bus.emit("state", self.snapshot())

    # ---------- 设置 ----------
    async def apply_settings(self, patch: dict) -> Settings:
        old_sess = self.settings.effective_sessdata()
        old_records = str(self.settings.records_dir)
        old_weak = str(self.settings.weak_dir)
        old_log = str(self.settings.log_path())
        s = self.settings_store.update(patch)
        self.settings = s
        # 日志目录变更：热切换文件句柄，不影响录制任务
        if str(s.log_path()) != old_log:
            try:
                self.bus.configure_log_file(s.log_path())
                self.bus.log("info", f"日志目录已切换：{s.log_path()}")
            except OSError as e:
                self.bus.log("error", f"日志目录切换失败：{e}")
        path_changed = (str(s.records_dir) != old_records
                        or str(s.weak_dir) != old_weak)
        # SESSDATA 或存储路径变更：滚动重启运行中的房间使其生效
        if s.effective_sessdata() != old_sess or path_changed:
            running = [rid for rid in self.tasks if self.is_running(rid)]
            if running:
                reason = "登录凭证" if s.effective_sessdata() != old_sess else "存储路径"
                self.bus.log("info",
                             f"{reason}已变更，滚动重启 {len(running)} 个录制任务")
                for rid in running:
                    await self.stop_room(rid)
                    await asyncio.sleep(1)
                    await self.start_room(rid)
        self.bus.emit("state", self.snapshot())
        return s

    # ---------- 空闲看门狗 ----------
    async def _idle_watchdog(self) -> None:
        minutes = max(1, self.settings.idle_stop_minutes)
        self.bus.log("info",
                     f"空闲停止已启用：全部房间关播 {minutes} 分钟后自动停止")
        offline_since: float | None = None
        async with httpx.AsyncClient(headers=P.HEADERS, timeout=10) as http:
            while True:
                await asyncio.sleep(WATCHDOG_POLL_SEC)
                any_live = False
                for t in self.target_store.targets:
                    try:
                        d = (await http.get(
                            "https://api.live.bilibili.com/room/v1/Room/get_info",
                            params={"room_id": t.room_id})).json().get("data", {})
                        if d.get("live_status") == 1:
                            any_live = True
                            break
                    except Exception:
                        any_live = True  # 查询失败：不误停
                        break
                if any_live:
                    if offline_since is not None:
                        self.bus.log("info", "检测到房间开播，关播计时已重置")
                    offline_since = None
                    continue
                now = time.time()
                if offline_since is None:
                    offline_since = now
                    self.bus.log("info",
                                 f"全部房间已关播，{minutes} 分钟后自动停止")
                elif now - offline_since >= minutes * 60:
                    self.bus.log("warning",
                                 f"全部房间关播已满 {minutes} 分钟，自动停止录制")
                    await self.stop_all()
                    return

    # ---------- 状态聚合 ----------
    def snapshot(self) -> dict:
        rooms = [self.runtimes[t.room_id].snapshot()
                 if t.room_id in self.runtimes else
                 self._make_rt(t).snapshot()
                 for t in self.target_store.targets]
        totals = {"danmaku": 0, "super_chat": 0, "gift": 0,
                  "emoticon_skipped": 0, "moderation": 0}
        running = connected = live = 0
        video_active = video_armed = 0
        rate = 0
        for r in rooms:
            if r["running"]:
                running += 1
            if r["connected"]:
                connected += 1
            if r["live_status"] == 1 and r["running"]:
                live += 1
            # 视频录制独立计数：active=视频流正在落盘；armed=任务运行中且该房间已勾选视频
            if r.get("video_active"):
                video_active += 1
            if r["running"] and r.get("record_video"):
                video_armed += 1
            rate += r["rate_per_min"]
            for k in totals:
                totals[k] += r["counters"].get(k, 0)
        return {
            "server_time": time.time(),
            "uptime": int(time.time() - self.started_at) if self.started_at else 0,
            "recording": running > 0,
            "running_count": running,
            "connected_count": connected,
            "live_count": live,
            "video_active_count": video_active,
            "video_armed_count": video_armed,
            "rate_per_min": rate,
            "totals": totals,
            "settings": self.settings.public_dict(),
            "rooms": rooms,
        }
