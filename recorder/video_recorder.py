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
import shutil
import time
from datetime import datetime
from pathlib import Path

import httpx

from . import protocol as P
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


# ---------------- 协程入口 ----------------

async def video_supervise(rt: RoomRuntime, session: LiveRoomSession,
                          settings: Settings, bus: EventBus) -> None:
    """视频录制监督协程：等开播→录一场→下播收尾，循环至任务被取消。"""
    poll_sec = max(10, settings.offline_poll_sec)
    try:
        while True:
            # ---- 门控：未开播轮询；开关关闭时短暂等待（房间开关可运行中动态切换）----
            while session.live_status != 1 or not rt.record_video:
                if session.live_status != 1:
                    await asyncio.sleep(poll_sec)
                    try:
                        await asyncio.to_thread(session.refresh_info)
                    except Exception:
                        pass  # 查询失败保守保持原状态
                else:
                    await asyncio.sleep(2)
            ended = await _run_one_session(rt, session, settings, bus)
            # 正常下播且开了 AI 自动解读：异步跑 LLM（失败只告警，不影响录制循环）
            if (ended and ended.get("reason") == "live_end"
                    and settings.llm_auto_analyze and settings.llm_ready()):
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
    out_dir = settings.video_dir / rt.vup
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
    out_dir = settings.video_dir / rt.vup
    try:
        play = await asyncio.to_thread(session.get_play_info, settings.video_qn)
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
        "requested_qn": settings.video_qn,
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
    if play["current_qn"] < settings.video_qn:
        downgrade = (f"（请求 {VIDEO_QN_LABELS.get(settings.video_qn, settings.video_qn)}"
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
                        session.get_play_info, settings.video_qn)
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
                        session.get_play_info, settings.video_qn)
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


# ---------------- ASS 旁挂字幕 ----------------

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


def _lane_hash(uid: str) -> int:
    h = 0
    for ch in uid or "?":
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return h


def generate_ass(jsonl_path: Path, ass_path: Path, meta: dict) -> str | None:
    """从同日 jsonl 抽取本场时间窗内的弹幕/SC，生成旁挂 ASS 字幕。

    返回生成的文件名；无任何事件时返回 None（不写空文件）。
    """
    t0 = float(meta.get("start_ts") or 0)
    t1 = float(meta.get("end_ts") or time.time())
    if not t0 or not jsonl_path.exists():
        return None
    rows: list[tuple[float, str, str, str, float]] = []  # ts, uid, kind, text, price
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
                    rows.append((ts, str(rec.get("uid_anon") or ""),
                                 "danmaku", rec["text"], 0))
                elif typ == "super_chat" and rec.get("text"):
                    rows.append((ts, str(rec.get("uid_anon") or ""),
                                 "super_chat", rec["text"],
                                 float(rec.get("price_rmb") or 0)))
    except OSError:
        return None
    if not rows:
        return None

    # 颜色为 &HAABBGGRR
    header = f"""[Script Info]
Title: {meta.get('base', 'live')} {_ass_escape(meta.get('title', ''))}
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720
WrapStyle: 2
ScaledBorderAndShadow: yes
Collisions: Normal

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Danmaku,Microsoft YaHei,24,&H00FFFFFF,&H00FFFFFF,&H66000000,&H99000000,0,0,0,0,100,100,0,0,1,1.6,0,7,0,0,0,1
Style: SuperChat,Microsoft YaHei,30,&H0030D7FF,&H0030D7FF,&H66000000,&HAA000000,1,0,0,0,100,100,0,0,1,2,0,8,20,20,26,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines: list[str] = []
    LANES = 11
    TRACK_ROW = 54
    lane_free = [0.0] * LANES  # 每条轨道下次可用时间（相对秒）
    DANMAKU_DUR = 8.0
    SC_DUR = 10.0
    for ts, uid, kind, text, price in rows:
        rel = ts - t0
        text = _ass_escape(text)[:80]
        if not text:
            continue
        if kind == "danmaku":
            width = sum(24 if ord(c) > 0x2E7F else 12 for c in text)
            lane = _lane_hash(uid) % LANES
            # 轨道被占用时顺延到最早空闲轨道，减少重叠
            if lane_free[lane] > rel:
                lane = min(range(LANES), key=lambda i: lane_free[i])
            y = 40 + lane * TRACK_ROW
            lane_free[lane] = rel + DANMAKU_DUR * 0.35
            lines.append(
                f"Dialogue: 0,{_ass_time(rel)},{_ass_time(rel + DANMAKU_DUR)},"
                f"Danmaku,,0,0,0,,{{\\move(1300,{y},{-width},{y})}}{text}")
        else:
            prefix = f"【SC ¥{price:g}】" if price else "【SC】"
            lines.append(
                f"Dialogue: 1,{_ass_time(rel)},{_ass_time(rel + SC_DUR)},"
                f"SuperChat,,0,0,0,,{prefix}{text}")
    ass_path.parent.mkdir(parents=True, exist_ok=True)
    ass_path.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    return ass_path.name
