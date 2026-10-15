"""直播视频直录：HTTP-FLV 流式落盘（零转码、零新依赖）。

与弹幕录制解耦的独立协程，产物落在 data/video/<vup>/：
  live_YYYYMMDD_HHMM.flv        首段纯净 FLV（剪辑用，不烧弹幕）
  live_YYYYMMDD_HHMM.pNN.flv   断线重连后的分段（时间戳不连续，B 档再 concat）
  live_YYYYMMDD_HHMM.meta.json 场次元数据（分段/时间戳/画质，文件管理与高能对齐用）
  live_YYYYMMDD_HHMM.ass        旁挂弹幕字幕（场次结束时由同日 jsonl 生成）

生产策略（沿用弹幕链路已验证的思路）：
- 开关双层：settings.video_enabled（全局，任务快照）+ rt.record_video（房间级，可动态切换）
- CDN url_info 多节点轮换；碎段（<5s）丢弃；全部节点快败则退避后重新拉 play info
- 磁盘剩余低于阈值自动停录并告警，冷却后复检
- 显式区分 CancelledError（用户停录/任务结束，保留文件）与流异常（自动重连）
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

import httpx

from . import flv_meta, media, protocol as P
from .config import VIDEO_QN_LABELS, Settings
from .events import EventBus
from .models import RoomRuntime
from .session import LiveRoomSession

CONNECT_TIMEOUT = 15
READ_TIMEOUT = 30            # FLV 持续有数据流；30s 无字节视为僵死，重连
CHUNK_BYTES = 64 * 1024
MIN_SEG_SEC = 5              # 短于 5s 的分段视为碎段丢弃
DISK_CHECK_BYTES = 8 * 1024 * 1024
DISK_RETRY_SEC = 300
PLAY_RETRY_SEC = 20
RECONNECT_BASE = 2
RECONNECT_CAP = 30

FLV_MAGIC = b"FLV\x01"


class DiskLowError(RuntimeError):
    """磁盘剩余空间低于阈值。"""


# ---------------- 场次元数据 ----------------

def meta_path_for(out_dir: Path, base: str) -> Path:
    return out_dir / f"{base}.meta.json"


def write_meta(meta: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(path)


def load_meta(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _free_gb(path: Path) -> float:
    try:
        return shutil.disk_usage(path).free / (1024 ** 3)
    except OSError:
        return 9999.0


def _eff_video_qn(rt: RoomRuntime, settings: Settings) -> int:
    """房间覆盖画质（None=跟随全局）；下次取流时读取，切换自然在重连生效。"""
    return settings.video_qn if rt.ov_video_qn is None else rt.ov_video_qn


def _eff_auto_analyze(rt: RoomRuntime, settings: Settings) -> bool:
    if rt.ov_llm_auto_analyze is None:
        return settings.llm_auto_analyze
    return rt.ov_llm_auto_analyze


# ---------------- 协程入口 ----------------

async def video_supervise(rt: RoomRuntime, session: LiveRoomSession,
                          settings: Settings, bus: EventBus,
                          global_on: Callable[[], bool] | None = None) -> None:
    """视频录制监督协程：等开关齐备→等开播→录一场→下播收尾，循环至任务被取消。

    全局开关经 global_on() 实时读取（设置页切换无需重启任务）；房间开关
    rt.record_video 同样可运行中动态切换。两个开关都打开且主播正在直播时，
    最迟 2s 内起录；未开播则零额外请求地等待，开播后自动起录。
    """
    if global_on is None:  # 未提供实时读取时回退任务快照语义
        global_on = lambda: settings.video_enabled
    poll_sec = max(10, settings.offline_poll_sec)
    try:
        while True:
            # ---- 门控 1：等两个开关齐备（纯本地状态，睡眠零网络请求）----
            while not rt.record_video or not global_on():
                await asyncio.sleep(2)
            # ---- 门控 2：开关齐备后才轮询开播状态（避免未开视频的房间白白发请求）----
            while session.live_status != 1:
                await asyncio.sleep(poll_sec)
                try:
                    await asyncio.to_thread(session.refresh_info)
                except Exception:
                    pass  # 查询失败保守保持原状态
                if not rt.record_video or not global_on():
                    break  # 等待开播期间开关被关：回到门控 1 重新等
            else:
                # live_status==1（含任务启动时主播已在播、运行中才打开开关）：立即起录
                ended = await _run_one_session(rt, session, settings, bus)
                # 正常下播且开了 AI 自动解读：异步跑 LLM（失败只告警，不影响录制循环）
                # 该开关支持房间级覆盖（rt.ov_llm_auto_analyze）
                if (ended and ended.get("reason") == "live_end"
                        and _eff_auto_analyze(rt, settings)
                        and settings.llm_ready()):
                    await _auto_analyze_session(rt, settings, bus, ended)
    except asyncio.CancelledError:
        bus.log("info", f"[{rt.vup}] 视频录制协程已停止", rt.room_id)
        raise


async def _auto_analyze_session(rt: RoomRuntime, settings: Settings,
                                bus: EventBus, ended: dict) -> None:
    """场次正常结束后的 AI 自动解读（整体限时 4 分钟，结果旁挂 ai.json）。"""
    # 延迟导入：LLM 是可选增强层，导入失败不应影响录制主链路
    from recorder import highlights as highlights_mod
    from recorder import llm_analyze as llm_mod

    base = ended["base"]
    out_dir = settings.video_path() / rt.vup
    meta = load_meta(meta_path_for(out_dir, base))
    if not meta:
        return
    jsonl = settings.records_dir / rt.vup / str(ended.get("jsonl") or "")
    if not jsonl.is_file():
        return
    ai_path = out_dir / f"{base}.ai.json"
    bus.log("info",
            f"[{rt.vup}] 🤖 场次已结束，开始 AI 自动解读"
            f"（{settings.llm_mode} 档 / {settings.llm_model}）",
            rt.room_id)
    try:
        rule = await asyncio.to_thread(
            highlights_mod.detect_highlights, jsonl, meta, 60, 8)
        await asyncio.wait_for(
            llm_mod.analyze_session(
                settings=settings, jsonl_path=jsonl, meta=meta,
                rule_result=rule, mode=settings.llm_mode,
                ai_path=ai_path, force=False),
            timeout=240)
    except asyncio.TimeoutError:
        bus.log("warning", f"[{rt.vup}] AI 自动解读超时（>4 分钟），已跳过",
                rt.room_id)
        return
    except Exception as e:
        bus.log("warning",
                f"[{rt.vup}] AI 自动解读失败：{str(e)[:100]}", rt.room_id)
        return
    bus.log("info", f"[{rt.vup}] 🤖 AI 自动解读完成：{base}", rt.room_id)
    bus.emit("video", {"room_id": rt.room_id, "vup": rt.vup,
                       "status": "ai_done", "session": base})


async def _run_one_session(rt: RoomRuntime, session: LiveRoomSession,
                           settings: Settings, bus: EventBus) -> dict | None:
    """录一场。正常结束返回 {reason,base,jsonl}；提前放弃（取流失败/目录不可写）返回 None。"""
    out_dir = settings.video_path() / rt.vup
    want_qn = _eff_video_qn(rt, settings)
    try:
        play = await asyncio.to_thread(session.get_play_info, want_qn)
    except Exception as e:
        rt.video_error = str(e)[:120]
        bus.log("warning",
                f"[{rt.vup}] 获取视频流地址失败：{str(e)[:80]}，"
                f"{PLAY_RETRY_SEC}s 后重试", rt.room_id)
        bus.emit("room_state", rt.snapshot())
        await asyncio.sleep(PLAY_RETRY_SEC)
        return

    urls = list(play["urls"])
    start_ts = time.time()
    base = f"live_{datetime.now():%Y%m%d_%H%M}"
    jsonl_name = f"live_{datetime.fromtimestamp(start_ts):%Y%m%d}.jsonl"
    meta: dict = {
        "_type": "video_session",
        "version": 1,
        "room_id": session.real_room_id,
        "vup": rt.vup,
        "anchor_name": session.anchor_name,
        "title": session.title,
        "base": base,
        "start_ts": start_ts,
        "start_clock": datetime.fromtimestamp(start_ts)
            .strftime("%Y-%m-%d %H:%M:%S"),
        "end_ts": None,
        "qn": play["current_qn"],
        "qn_label": VIDEO_QN_LABELS.get(play["current_qn"],
                                        str(play["current_qn"])),
        "requested_qn": want_qn,
        "protocol": play["protocol"],
        "format": play["format"],
        "codec": play["codec"],
        "jsonl": jsonl_name,
        "segments": [],
        "ass": None,
    }
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        rt.video_error = f"视频目录创建失败: {e}"
        bus.log("error", f"[{rt.vup}] 视频目录不可写：{e}", rt.room_id)
        await asyncio.sleep(PLAY_RETRY_SEC)
        return
    mpath = meta_path_for(out_dir, base)
    write_meta(meta, mpath)

    rt.video_active = True
    rt.video_session = base
    rt.video_started_at = start_ts
    rt.video_qn = play["current_qn"]
    rt.video_error = ""
    downgrade = ""
    if play["current_qn"] < want_qn:
        downgrade = (f"（请求 {VIDEO_QN_LABELS.get(want_qn, want_qn)}"
                     f"被降档为 {meta['qn_label']}，登录有效 SESSDATA 可拿原画）")
    bus.log("info",
            f"[{rt.vup}] ▶ 视频开播直录：{base}.{play['format']} "
            f"[{meta['qn_label']}/{play['codec'].upper()}/{len(urls)} 个CDN]{downgrade}",
            rt.room_id)
    bus.emit("video", {"room_id": rt.room_id, "vup": rt.vup,
                       "status": "session_start", "session": base,
                       "qn": play["current_qn"]})
    bus.emit("room_state", rt.snapshot())

    headers = dict(P.HEADERS)
    cookie = session.ws_cookie_header()
    if cookie:
        headers["Cookie"] = cookie
    headers["Referer"] = f"https://live.bilibili.com/{rt.room_id}"

    end_reason = "live_end"
    node_idx = 0
    fast_fails = 0
    try:
        while True:
            free = _free_gb(out_dir)
            if free < settings.video_min_free_gb:
                end_reason = "disk"
                rt.video_error = (f"磁盘剩余 {free:.1f}GB 低于阈值 "
                                  f"{settings.video_min_free_gb}GB，已自动停录")
                bus.log("error",
                        f"[{rt.vup}] ⚠ 磁盘剩余仅 {free:.1f}GB，"
                        f"视频自动停录并告警；{DISK_RETRY_SEC // 60} 分钟后复检",
                        rt.room_id)
                bus.emit("video", {"room_id": rt.room_id, "vup": rt.vup,
                                   "status": "disk_low", "session": base,
                                   "free_gb": round(free, 1)})
                break

            kept = len(meta["segments"])
            seg_name = f"{base}.flv" if kept == 0 else f"{base}.p{kept + 1:02d}.flv"
            seg_path = out_dir / seg_name
            url = urls[node_idx % len(urls)]
            node_idx += 1
            rt.video_file = seg_name
            bus.emit("room_state", rt.snapshot())

            seg_start = time.time()
            outcome, detail = await _record_segment(
                rt, url, headers, seg_path, settings, out_dir)
            seg_end = time.time()
            seg_bytes = 0
            try:
                seg_bytes = seg_path.stat().st_size
            except OSError:
                pass

            if outcome == "ok":
                fast_fails = 0
            else:
                fast_fails += 1

            # 碎段丢弃（仅针对异常快断；正常下播/EVF 再短也保留）
            discarded = (outcome != "ok"
                         and seg_end - seg_start < MIN_SEG_SEC)
            if discarded:
                try:
                    seg_path.unlink(missing_ok=True)
                except OSError:
                    pass
                rt.video_bytes = max(0, rt.video_bytes - seg_bytes)

            seg_rec = {
                "file": seg_name,
                "start_ts": seg_start,
                "end_ts": seg_end,
                "bytes": seg_bytes,
                "node": url.split("/")[2] if "//" in url else "",
                "result": outcome,
                "detail": detail,
                "discarded": discarded,
            }
            meta["segments"].append(seg_rec)
            if not discarded:
                rt.video_segments = len([s for s in meta["segments"]
                                         if not s.get("discarded")])
            write_meta(meta, mpath)

            if outcome != "ok":
                bus.log("warning",
                        f"[{rt.vup}] 视频流中断（{detail}，"
                        f"{seg_end - seg_start:.0f}s/{seg_bytes // 1024}KB）"
                        + ("，碎段已丢弃，" if discarded else "，分段已保留，")
                        + "准备重连",
                        rt.room_id)

            # 判断是否真的下播（外部 API 是不可信源，这里只做一次确认）
            try:
                await asyncio.to_thread(session.refresh_info)
            except Exception:
                pass
            if session.live_status != 1:
                end_reason = "live_end"
                break

            # 仍在播 → 重连。全部 CDN 节点都快败过：退避并重新拉 play info
            if fast_fails > 0 and fast_fails % len(urls) == 0:
                cool = min(RECONNECT_BASE * 2 ** min(fast_fails - 1, 4),
                           RECONNECT_CAP) + random.randint(0, 3)
                bus.log("warning",
                        f"[{rt.vup}] 视频全部 {len(urls)} 个 CDN 节点快败，"
                        f"{cool}s 后重新获取流地址", rt.room_id)
                await asyncio.sleep(cool)
                try:
                    play = await asyncio.to_thread(
                        session.get_play_info, _eff_video_qn(rt, settings))
                    urls = list(play["urls"])
                    node_idx = 0
                    rt.video_qn = play["current_qn"]
                except Exception as e:
                    bus.log("warning",
                            f"[{rt.vup}] 重新获取视频流地址失败：{str(e)[:80]}，"
                            f"沿用旧地址重试", rt.room_id)
            else:
                wait = RECONNECT_BASE if outcome == "ok" else min(
                    RECONNECT_BASE * 2 ** min(fast_fails - 1, 4),
                    RECONNECT_CAP)
                await asyncio.sleep(wait + random.random())
                # 常规重连也刷新一次节点地址（URL 含时效参数）
                try:
                    play = await asyncio.to_thread(
                        session.get_play_info, _eff_video_qn(rt, settings))
                    urls = list(play["urls"])
                    node_idx = 0
                except Exception:
                    pass
    except asyncio.CancelledError:
        end_reason = "user_stop"
        raise
    finally:
        # 同步收尾：任务被取消时 await 会再次抛 CancelledError，故全部用同步 IO
        meta["end_ts"] = time.time()
        meta["end_reason"] = end_reason
        ass_path = out_dir / f"{base}.ass"
        try:
            ass_name = generate_ass(
                settings.records_dir / rt.vup / jsonl_name,
                ass_path, meta)
            meta["ass"] = ass_name
        except Exception as e:
            bus.log("warning",
                    f"[{rt.vup}] ASS 字幕生成失败：{str(e)[:80]}", rt.room_id)
        write_meta(meta, mpath)
        kept_segs = [s for s in meta["segments"] if not s.get("discarded")]
        total_bytes = sum(s["bytes"] for s in kept_segs)
        duration = int((meta["end_ts"] or start_ts) - start_ts)
        bus.log("info",
                f"[{rt.vup}] ■ 视频场次结束（{end_reason}）：{base} · "
                f"{len(kept_segs)} 段 / {total_bytes / 1024 / 1024:.1f}MB / "
                f"约 {duration // 60} 分钟，字幕 "
                f"{'已生成' if meta.get('ass') else '未生成'}",
                rt.room_id)
        bus.emit("video", {"room_id": rt.room_id, "vup": rt.vup,
                           "status": "session_end", "session": base,
                           "reason": end_reason,
                           "segments": len(kept_segs),
                           "bytes": total_bytes})
        rt.video_reset()
        bus.emit("room_state", rt.snapshot())
        if end_reason == "disk":
            # 冷却后再让监督协程重新门控，避免立即重新建流
            await asyncio.sleep(DISK_RETRY_SEC)

    return {"reason": end_reason, "base": base, "jsonl": jsonl_name}


async def _record_segment(rt: RoomRuntime, url: str, headers: dict,
                          seg_path: Path, settings: Settings,
                          out_dir: Path) -> tuple[str, str]:
    """拉一路 FLV 写一个分段。

    返回 (outcome, detail)：outcome=ok 表示服务端正常关闭连接（需重新判断下播/重连），
    其余为 http_error / bad_magic / timeout / network_error / disk_low。
    """
    timeout = httpx.Timeout(CONNECT_TIMEOUT, read=READ_TIMEOUT,
                            write=15, pool=15)
    bytes_since_check = 0
    last_flush = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout,
                                     follow_redirects=True) as client:
            async with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code != 200:
                    return ("http_error", f"HTTP {resp.status_code}")
                ctype = resp.headers.get("content-type", "")
                aiter = resp.aiter_bytes(CHUNK_BYTES)
                try:
                    first = await aiter.__anext__()
                except StopAsyncIteration:
                    return ("network_error", "响应无数据")
                except httpx.HTTPError as e:
                    return ("network_error", type(e).__name__)
                if not first.startswith(FLV_MAGIC):
                    return ("bad_magic",
                            f"非FLV数据 {first[:8]!r} ({ctype})")
                with seg_path.open("wb") as f:
                    f.write(first)
                    rt.video_bytes += len(first)
                    bytes_since_check += len(first)
                    async for chunk in aiter:
                        f.write(chunk)
                        rt.video_bytes += len(chunk)
                        bytes_since_check += len(chunk)
                        now = time.time()
                        if now - last_flush >= 2:
                            f.flush()
                            last_flush = now
                        if bytes_since_check >= DISK_CHECK_BYTES:
                            bytes_since_check = 0
                            if _free_gb(out_dir) < settings.video_min_free_gb:
                                raise DiskLowError("disk low")
                return ("ok", "eof")
    except DiskLowError:
        return ("disk_low", "磁盘空间不足")
    except httpx.ReadTimeout:
        return ("timeout", f"{READ_TIMEOUT}s 无数据")
    except httpx.HTTPError as e:
        return ("network_error", f"{type(e).__name__}: {str(e)[:60]}")
    except OSError as e:
        return ("io_error", f"{type(e).__name__}: {str(e)[:60]}")


# ---------------- ASS 旁挂弹幕字幕（v2：原生观感渲染） ----------------

def _ass_time(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _ass_escape(text: str) -> str:
    return (text.replace("{", "｛").replace("}", "｝")
                .replace("\r", " ").replace("\n", " ")
                .strip())


def _text_px(text: str, font_h: float) -> float:
    """估算文本像素宽：中文/全角 ≈ 1.02em，ASCII ≈ 0.55em（用于车道碰撞与折行）。"""
    return sum(font_h * 1.02 if ord(c) > 0x2E7F else font_h * 0.55
               for c in text)


def _ass_color(rgb: int, alpha: int = 0) -> str:
    """0xRRGGBB 整数 + alpha(0 不透 / 255 全透) → ASS &HAABBGGRR。"""
    r = (rgb >> 16) & 0xFF
    g = (rgb >> 8) & 0xFF
    b = rgb & 0xFF
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def _ass_hex(hex_color: str, alpha: int = 0) -> str:
    """#RRGGBB → ASS 颜色。"""
    return _ass_color(int(str(hex_color).lstrip("#"), 16), alpha)


def _wrap_text(text: str, font_h: float, max_w: float,
               max_lines: int = 6) -> list[str]:
    """按估算字宽逐字折行（CJK 可任意断点）；超出 max_lines 的部分截断。"""
    lines: list[str] = []
    cur = ""
    cur_w = 0.0
    for ch in text:
        if ch == "\t":
            ch = " "
        cw = font_h * (1.0 if ord(ch) > 0x2E7F else 0.55)
        if cur and cur_w + cw > max_w:
            lines.append(cur)
            if len(lines) >= max_lines:
                return lines
            cur, cur_w = "", 0.0
        cur += ch
        cur_w += cw
    if cur and len(lines) < max_lines:
        lines.append(cur)
    return lines


class _ScrollLane:
    """一条弹幕车道：最后一条滚动弹幕 + 顶/底固定弹幕占用区间表。"""

    __slots__ = ("last_t", "last_w", "fixed")

    def __init__(self) -> None:
        self.last_t: float | None = None
        self.last_w: float = 0.0
        self.fixed: list[tuple[float, float]] = []

    def scroll_ok(self, t: float, w: float, dur: float,
                  v: float, W: float, gap: float) -> bool:
        end = t + dur
        for fs, fe in self.fixed:
            if t < fe and fs < end:
                return False  # 可见区间与固定弹幕占用窗相交
        if self.last_t is None:
            return True
        # 同速弹幕间距恒定：新条锚点在 W，前条右边缘在 W-v·dt+last_w，
        # 不重叠要求 v·dt ≥ 前条宽 + gap（前条已完全出屏更无约束）
        dt = v * (t - self.last_t)
        return dt >= W or dt >= self.last_w + gap

    def scroll_emit_time(self, t: float, w: float, dur: float,
                         v: float, W: float, gap: float) -> float:
        """该车道最早可发射时刻（含固定窗避让），可能 > t。"""
        te = t
        if self.last_t is not None:
            need = (self.last_w + gap) / v
            if te < self.last_t + need and v * (te - self.last_t) < W:
                te = self.last_t + need
        # 固定窗避让：发射后整个可见区间不得与任何固定窗相交
        for _ in range(8):
            hit = None
            end = te + dur
            for fs, fe in self.fixed:
                if te < fe and fs < end:
                    hit = fe
                    break
            if hit is None:
                break
            te = max(te, hit)
        return te

    def add_fixed(self, t: float, dur: float) -> None:
        self.fixed.append((t, t + dur))
        # 只保留未过期的占用窗
        self.fixed = [(s, e) for s, e in self.fixed if e > t]

    def put_scroll(self, t: float, w: float) -> None:
        self.last_t, self.last_w = t, w


def _resolve_dims(ass_path: Path, meta: dict, style, ffmpeg_info) -> tuple[int, int, str]:
    """确定字幕画布尺寸，优先级：样式手动画布 > meta 缓存 > FLV 头部解析 >
    ffmpeg 探测 > 默认 1920×1080。探测成功的结果写入 meta['dims'] 缓存（不落盘，
    由调用方随 ass 字段一起 write_meta）。"""
    if style.canvas_mode == "manual":
        return style.canvas_w, style.canvas_h, "manual"
    cached = meta.get("dims")
    if isinstance(cached, dict):
        try:
            w, h = int(cached["w"]), int(cached["h"])
            if 160 <= w <= 7680 and 160 <= h <= 4320:
                return w, h, str(cached.get("source") or "cache")
        except (KeyError, TypeError, ValueError):
            pass
    base = ass_path.stem
    out_dir = ass_path.parent
    candidates: list[Path] = []
    for s in meta.get("segments", []):
        fn = str(s.get("file") or "")
        if fn and not s.get("discarded"):
            candidates.append(out_dir / fn)
    if not candidates:
        # meta 未收尾 / 进行中场次：按场次前缀认回第一个分段
        candidates = sorted(p for p in out_dir.glob(f"{base}*.flv")
                            if ".clip_" not in p.name)
    for p in candidates:
        try:
            if p.is_file():
                dims = flv_meta.probe_flv_file(p)
                if dims:
                    meta["dims"] = {"w": dims[0], "h": dims[1], "source": "flv"}
                    return dims[0], dims[1], "flv"
        except OSError:
            continue
    if ffmpeg_info:
        for p in candidates:
            try:
                if p.is_file():
                    dims = media.probe_dims(ffmpeg_info, p)
                    if dims:
                        meta["dims"] = {"w": dims[0], "h": dims[1],
                                        "source": "ffmpeg"}
                        return dims[0], dims[1], "ffmpeg"
            except OSError:
                continue
    return 1920, 1080, "default"


def _read_events(jsonl_path: Path, t0: float, t1: float) -> list[dict]:
    """扫描同日 jsonl，抽取本场时间窗内的弹幕/SC 事件（按 ts 稳定排序）。"""
    rows: list[dict] = []
    try:
        with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                ts = float(rec.get("ts") or 0)
                if not ts or ts < t0 - 1 or ts > t1 + 1:
                    continue
                typ = rec.get("_type")
                if typ == "danmaku" and rec.get("text"):
                    try:
                        color = int(rec.get("color") or 0)
                    except (TypeError, ValueError):
                        color = 0
                    try:
                        mode = int(rec.get("mode") or 1)
                    except (TypeError, ValueError):
                        mode = 1
                    rows.append({"ts": ts, "kind": "danmaku",
                                 "text": rec["text"], "color": color,
                                 "mode": mode})
                elif typ == "super_chat" and rec.get("text"):
                    rows.append({
                        "ts": ts, "kind": "super_chat",
                        "text": rec["text"],
                        "uname": str(rec.get("uname_anon") or ""),
                        "price": float(rec.get("price_rmb") or 0),
                        "sc_time": int(rec.get("sc_time") or 0),
                        "sc_bg": str(rec.get("sc_bg") or ""),
                        "sc_fg": str(rec.get("sc_fg") or ""),
                        "sc_price_color": str(rec.get("sc_price_color") or ""),
                    })
    except OSError:
        return []
    rows.sort(key=lambda r: r["ts"])
    return rows


def generate_ass(jsonl_path: Path, ass_path: Path, meta: dict,
                 style=None, ffmpeg_info=None) -> str | None:
    """从同日 jsonl 抽取本场时间窗内的弹幕/SC，生成原生观感旁挂 ASS 字幕。

    返回生成的文件名；无任何事件时返回 None（不写空文件）。
    style 缺省读取全局弹幕样式（config/danmaku_style.json，改完即生效）；
    ffmpeg_info 缺省时内部检测（仅在 FLV 头部解析不到尺寸时兜底用）。
    """
    from .danmaku_style import get_store, sc_tier_colors

    if style is None:
        style = get_store().style
    if ffmpeg_info is None:
        ffmpeg_info = media.find_ffmpeg()
    t0 = float(meta.get("start_ts") or 0)
    t1 = float(meta.get("end_ts") or time.time())
    if not t0 or not jsonl_path.exists():
        return None
    rows = _read_events(jsonl_path, t0, t1)
    if not rows:
        return None

    W, H, dims_src = _resolve_dims(ass_path, meta, style, ffmpeg_info)
    # ---- 布局常量（全部随真实分辨率自适应）----
    S = max(16, round(H / 28 * float(style.font_scale)))
    LH = round(S * 1.25)
    margin = max(6, round(H * 0.02))
    lanes_n = max(1, int((H * float(style.area_ratio) - margin) // LH))
    T = style.cross_seconds()
    v = W / T                      # 像素/秒，所有滚动弹幕等速
    gap = S * 0.25
    alpha = round(255 * (1.0 - float(style.opacity)))
    outline = int(style.outline)
    bold = -1 if style.bold else 0
    ML = MR = max(8, round(W * 0.01))
    FIXED_DUR = 5.0
    MAX_DELAY = 2.0

    header = f"""[Script Info]
Title: {meta.get('base', 'live')} {_ass_escape(meta.get('title', ''))}
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes
Collisions: Normal

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Danmaku,{style.font_family},{S},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,{bold},0,0,0,100,100,0,0,1,{outline},0,7,{ML},{MR},0,1
Style: FixedTop,{style.font_family},{S},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,{bold},0,0,0,100,100,0,0,1,{outline},0,8,{ML},{MR},{margin},1
Style: FixedBottom,{style.font_family},{S},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,{bold},0,0,0,100,100,0,0,1,{outline},0,2,{ML},{MR},{margin},1
Style: SCPlain,{style.font_family},{round(S * 1.05)},&H0030D7FF,&H0030D7FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,{max(1, outline)},0,2,{ML},{MR},{round(H * 0.06)},1
Style: SCDraw,{style.font_family},{S},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lanes = [_ScrollLane() for _ in range(lanes_n)]
    events: list[tuple[float, str]] = []   # (start_sec, Dialogue 行)

    # SC 卡片栈：活动卡片（底边随新卡上推，旧卡用切段实现位置跳变）
    CW = min(round(W * 0.42), 800)
    cw_xr = W - max(8, round(W * 0.02))
    cw_xl = cw_xr - CW
    bar_w = max(4, round(S * 0.09))
    pad = round(S * 0.5)
    gap_v = round(S * 0.35)
    tx = cw_xl + pad + bar_w + round(S * 0.35)
    text_w = CW - (tx - cw_xl) - pad
    hs = round(S * 0.72)          # 标题行字号
    bs = round(S * 0.92)          # 正文字号
    header_h = round(hs * 1.4)
    body_lh = round(bs * 1.3)
    active_sc: list[dict] = []    # {t,end,bottom,segs:[(start,end,bottom)]}
    done_sc: list[dict] = []      # 已自然到期的卡：时间轴 Dialogue 仍须渲染

    def _hex6(v) -> bool:
        return isinstance(v, str) and bool(re.fullmatch(r"#[0-9a-fA-F]{6}", v))

    def emit_sc_card(ev: dict, t: float) -> None:
        nonlocal active_sc
        expired = [c for c in active_sc if c["end"] <= t]
        if expired:
            done_sc.extend(expired)
        active_sc = [c for c in active_sc if c["end"] > t]
        bg_hex = ev["sc_bg"]
        accent = ev["sc_price_color"]
        if not _hex6(bg_hex):
            bg_hex, accent0 = sc_tier_colors(ev["price"])
            accent = accent if _hex6(accent) else accent0
        elif not _hex6(accent):
            accent = sc_tier_colors(ev["price"])[1]
        fg = ev["sc_fg"] if _hex6(ev["sc_fg"]) else "#FFFFFF"
        dur = max(5, min(ev["sc_time"] or 10, 300))
        uname = _ass_escape(ev["uname"])[:24] or "观众"
        body_lines = _wrap_text(_ass_escape(ev["text"])[:120], bs, text_w)
        ch = 2 * pad + header_h + max(1, len(body_lines)) * body_lh
        # 旧卡全部上移（在 t 时刻切段：前段止于 t，新段从 t 起到各自 end）
        # 最新一张旧卡贴在新卡顶边之上，再依次按各自卡高向上排
        new_bottom = H - max(8, round(H * 0.03)) - ch - gap_v
        for c in reversed(active_sc):
            top_seg = c["segs"][-1]
            top_seg["end"] = t
            c["segs"].append({"start": t, "end": c["end"],
                              "bottom": new_bottom})
            new_bottom -= int(c["geom"]["ch"]) + gap_v
        card = {"t": t, "end": t + dur, "bottom": H - max(8, round(H * 0.03)),
                "segs": [{"start": t, "end": t + dur,
                          "bottom": H - max(8, round(H * 0.03))}]}
        active_sc.append(card)
        # 几何在所有卡片位置确定后统一落行（渲染放到扫描结束后）
        card["geom"] = {
            "bg": bg_hex, "accent": accent, "fg": fg,
            "uname": uname, "price": ev["price"],
            "lines": body_lines, "ch": ch,
        }

    def flush_sc_cards(cards: list[dict]) -> None:
        for c in cards:
            g = c["geom"]
            for seg in c["segs"]:
                if seg["end"] <= seg["start"]:
                    continue
                yb = seg["bottom"]
                yt = yb - g["ch"]
                st, en = _ass_time(seg["start"]), _ass_time(seg["end"])
                events.append((seg["start"],
                    f"Dialogue: 2,{st},{en},SCDraw,,0,0,0,,"
                    f"{{\\p1\\an7\\pos(0,0)\\bord0\\shad0\\1c"
                    f"{_ass_hex(g['bg'])}&\\1a&H26&}}"
                    f"m {cw_xl} {yt} l {cw_xr} {yt} l {cw_xr} {yb} "
                    f"l {cw_xl} {yb}{{\\p0}}"))
                events.append((seg["start"],
                    f"Dialogue: 2,{st},{en},SCDraw,,0,0,0,,"
                    f"{{\\p1\\an7\\pos(0,0)\\bord0\\shad0\\1c"
                    f"{_ass_hex(g['accent'])}&\\1a&H00&}}"
                    f"m {cw_xl} {yt} l {cw_xl + bar_w} {yt} "
                    f"l {cw_xl + bar_w} {yb} l {cw_xl} {yb}{{\\p0}}"))
                ty = yt + pad // 2
                events.append((seg["start"],
                    f"Dialogue: 3,{st},{en},SCDraw,,0,0,0,,"
                    f"{{\\an7\\pos({tx},{ty})\\bord1\\shad0\\fs{hs}"
                    f"\\1c{_ass_hex(g['accent'])}&}}"
                    f"{g['uname']} · ¥{g['price']:g}"))
                bx = ty + header_h
                body = "\\N".join(g["lines"])
                events.append((seg["start"],
                    f"Dialogue: 3,{st},{en},SCDraw,,0,0,0,,"
                    f"{{\\an7\\pos({tx},{bx})\\bord1\\shad0\\fs{bs}"
                    f"\\1c{_ass_hex(g['fg'])}&}}{body}"))

    for ev in rows:
        rel = ev["ts"] - t0
        if ev["kind"] == "super_chat":
            if style.sc_card:
                emit_sc_card(ev, rel)
            else:
                # 卡片关闭：SC 作为底部固定金色字幕，占用底部车道 10s
                text = _ass_escape(
                    f"【SC ¥{ev['price']:g} {ev['uname']}】{ev['text']}")[:120]
                w = _text_px(text, S * 1.05)
                for lane in range(lanes_n - 1, -1, -1):
                    if all(not (rel < fe and fs < rel + 10.0)
                           for fs, fe in lanes[lane].fixed):
                        y = H - margin - (lanes_n - 1 - lane) * LH
                        lanes[lane].add_fixed(rel, 10.0)
                        color = _ass_color(0xFFD730, 0)
                        events.append((rel,
                            f"Dialogue: 1,{_ass_time(rel)},"
                            f"{_ass_time(rel + 10.0)},SCPlain,,0,0,0,,"
                            f"{{\\an2\\pos({W // 2},{y})\\1c{color}}}{text}"))
                        break
            continue

        # ---- 普通弹幕 ----
        text = _ass_escape(ev["text"])[:120]
        if not text:
            continue
        w = _text_px(text, S)
        dur = (W + w) / v
        mode = ev["mode"] if style.fixed_mode else 1
        if mode == 5:  # 顶部固定
            placed = False
            for lane in range(lanes_n):
                if all(not (rel < fe and fs < rel + FIXED_DUR)
                       for fs, fe in lanes[lane].fixed):
                    y = margin + lane * LH
                    lanes[lane].add_fixed(rel, FIXED_DUR)
                    rgb = ev["color"] if (style.show_color and ev["color"]) else 0xFFFFFF
                    events.append((rel,
                        f"Dialogue: 1,{_ass_time(rel)},{_ass_time(rel + FIXED_DUR)},"
                        f"FixedTop,,0,0,0,,{{\\an8\\pos({W // 2},{y})"
                        f"\\1c{_ass_color(rgb, alpha)}}}{text}"))
                    placed = True
                    break
            continue
        if mode == 4:  # 底部固定
            placed = False
            for lane in range(lanes_n - 1, -1, -1):
                if all(not (rel < fe and fs < rel + FIXED_DUR)
                       for fs, fe in lanes[lane].fixed):
                    y = H - margin - (lanes_n - 1 - lane) * LH
                    lanes[lane].add_fixed(rel, FIXED_DUR)
                    rgb = ev["color"] if (style.show_color and ev["color"]) else 0xFFFFFF
                    events.append((rel,
                        f"Dialogue: 1,{_ass_time(rel)},{_ass_time(rel + FIXED_DUR)},"
                        f"FixedBottom,,0,0,0,,{{\\an2\\pos({W // 2},{y})"
                        f"\\1c{_ass_color(rgb, alpha)}}}{text}"))
                    placed = True
                    break
            continue
        # ---- 滚动（mode 1/2/3 及其它）----
        lane_idx = -1
        for i, lane in enumerate(lanes):
            if lane.scroll_ok(rel, w, dur, v, W, gap):
                lane_idx = i
                break
        emit_t = rel
        if lane_idx < 0:
            # 全车道暂不可用：选最早可发射车道，延后 ≤2s，再超则丢弃
            best_i, best_t = 0, float("inf")
            for i, lane in enumerate(lanes):
                te = lane.scroll_emit_time(rel, w, dur, v, W, gap)
                if te < best_t:
                    best_t, best_i = te, i
            if best_t - rel > MAX_DELAY:
                continue
            lane_idx, emit_t = best_i, best_t
        y = margin + lane_idx * LH + max(0, (LH - S) // 2)
        lanes[lane_idx].put_scroll(emit_t, w)
        rgb = ev["color"] if (style.show_color and ev["color"]) else 0xFFFFFF
        events.append((emit_t,
            f"Dialogue: 0,{_ass_time(emit_t)},{_ass_time(emit_t + dur)},"
            f"Danmaku,,0,0,0,,{{\\an7\\move({W},{y},{-round(w)},{y})"
            f"\\1c{_ass_color(rgb, alpha)}}}{text}"))

    # 已自然到期与仍存活的卡片都要渲染（到期仅表示时间轴停留结束）
    flush_sc_cards(done_sc + active_sc)

    events.sort(key=lambda x: (x[0], 0))
    ass_path.parent.mkdir(parents=True, exist_ok=True)
    ass_path.write_text(
        header + "\n".join(line for _, line in events) + "\n",
        encoding="utf-8")
    return ass_path.name
