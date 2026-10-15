"""单房间录制协程。

由 scripts/record_live_danmaku.py 实测版本重构而来，保留全部生产验证过的策略：
- 未开播不持有 WS（45s 轮询），开播才连，下播立刻断线释放账号并发名额
- host_list 多节点轮换；秒踢（<15s）指数退避 + 抖动；全节点秒踢熔断 60-90s
- getDanmuInfo -352 风控时重建指纹与 wbi key
- 错峰握手、应用层心跳 25s、ping_interval=None
- 懒落盘：仅 live_status==1 真实开播才建文件
- 房管拉黑/禁言事件回捞近 30 分钟被处置者弹幕 + 前 20 条语境（弱标签）
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import random
import re
import struct
import time
import zlib
from collections import deque
from dataclasses import replace
from datetime import datetime
from typing import Callable

import websockets

from . import filters as F
from . import protocol as P
from . import storage
from .config import Settings
from .events import EventBus
from .models import RoomRuntime
from .session import LiveRoomSession
from .video_recorder import video_supervise

# ---- 房管处置弱标签：滚动缓冲参数 ----
RECENT_MAX_N = 5000
RECENT_WINDOW_SEC = 1800
CONTEXT_N = 20
EVENT_DEDUP_SEC = 600


def _effective_opts(rt: RoomRuntime, settings: Settings) -> dict:
    """合并房间级覆盖（rt.ov_*，None=跟随全局快照）后的事件开关。

    在每条 WS 消息分发前调用：房间设置经 PUT 改动后下一条消息即生效。
    """
    def eff(ov_name: str, setting_name: str) -> bool:
        v = getattr(rt, ov_name)
        return getattr(settings, setting_name) if v is None else v

    return {
        "record_sc": eff("ov_record_sc", "record_sc"),
        "record_gifts": eff("ov_record_gifts", "record_gifts"),
        "record_moderation": eff("ov_record_moderation", "record_moderation"),
        "filter_pure_emoticon": eff("ov_filter_pure_emoticon",
                                    "filter_pure_emoticon"),
        "anonymize": eff("ov_anonymize", "anonymize"),
        "sanitize_text": eff("ov_sanitize_text", "sanitize_text"),
    }


async def record_room(rt: RoomRuntime, settings: Settings,
                      bus: EventBus,
                      video_enabled: Callable[[], bool] | None = None) -> None:
    """录制一个房间，直到被 manager 取消。

    settings 为启动时快照；rt.ov_* 房间级覆盖由管理器随 PUT 动态更新，
    事件类开关每条消息实时解析，无需重启任务。
    video_enabled 返回全局视频开关的实时值，使设置页开关切换对运行中
    任务即时生效（正在直播立即起录）；不传则回退快照语义。
    """
    sessdata = settings.effective_sessdata()
    salt = settings.effective_salt()
    records_dir = settings.records_dir
    weak_dir = settings.weak_dir
    poll_sec = max(10, settings.offline_poll_sec)

    rt.running = True
    rt.started_at = time.time()
    session: LiveRoomSession | None = None
    video_task: asyncio.Task | None = None
    try:
        try:
            session = LiveRoomSession(rt.room_id, sessdata)
        except Exception as e:
            rt.last_error = f"初始化失败: {e}"
            bus.log("error", f"[{rt.vup}] 会话初始化失败: {e}", rt.room_id)
            return

        rt.anchor_name = session.anchor_name
        rt.live_status = session.live_status
        rt.title = session.title
        bus.emit("room_state", rt.snapshot())
        bus.log("info",
                f"[{rt.vup}] room={session.real_room_id} "
                f"status={rt.live_status} title={session.title[:30]!r}"
                + ("" if sessdata else "（游客模式，稳定性可能较差）"),
                rt.room_id)

        # 视频直录：与弹幕 WS 并联的独立协程，始终启动；
        # 协程内部按「全局实时开关 + 房间动态开关」门控，未开启时零网络开销
        video_task = asyncio.create_task(
            video_supervise(rt, session, settings, bus, global_on=video_enabled),
            name=f"video-{rt.room_id}")

        def _video_done(t: asyncio.Task) -> None:
            if t.cancelled():
                return
            if t.exception():
                bus.log("error",
                        f"[{rt.vup}] 视频录制协程异常退出: "
                        f"{type(t.exception()).__name__}: {t.exception()}",
                        rt.room_id)

        video_task.add_done_callback(_video_done)

        state: dict = {
            "recent": deque(maxlen=RECENT_MAX_N),  # (uid明文, rec)
            "recent_events": {},                    # uid明文 -> 上次处置 ts
        }
        if session.live_status == 1:
            f = storage.ensure_out_file(session, rt, records_dir)
            if f:
                bus.log("info", f"[{rt.vup}] *** 开播落盘 *** {rt.current_file}",
                        rt.room_id)

        retry = 0
        fast_fails = 0
        token, hosts, host_idx = None, [], 0
        while True:
            # ---- 未开播门控：不持有 WS，轮询等开播 ----
            if session.live_status != 1:
                rt.connected = False
                bus.emit("room_state", rt.snapshot())
                while session.live_status != 1:
                    await asyncio.sleep(poll_sec)
                    try:
                        session.refresh_info()
                    except Exception:
                        pass  # 查询失败保守不切换状态
                    if session.live_status == 1:
                        rt.title = session.title
                        rt.anchor_name = session.anchor_name
                        bus.log("info",
                                f"[{rt.vup}] 轮询检测到开播：{session.title[:30]!r}",
                                rt.room_id)
                bus.emit("room_state", rt.snapshot())
                token, hosts, host_idx = None, [], 0

            if token is None or host_idx >= len(hosts):
                # 熔断：上一轮全部节点秒踢（典型=在播数超出账号并发上限）
                if hosts and fast_fails >= len(hosts):
                    cool = 60 + random.randint(0, 30)
                    bus.log("warning",
                            f"[{rt.vup}] 全部 {len(hosts)} 节点均秒踢，"
                            f"熔断冷却 {cool}s", rt.room_id)
                    await asyncio.sleep(cool)
                    fast_fails = 0
                try:
                    token, hosts = session.get_ws_info()
                    host_idx = 0
                    rt.host_count = len(hosts)
                    rt.last_error = ""
                except Exception as e:
                    msg = str(e)
                    rt.last_error = msg[:120]
                    if "-352" in msg:
                        bus.log("warning",
                                f"[{rt.vup}] getDanmuInfo -352 风控，"
                                f"重建签名凭证，60s 后重试", rt.room_id)
                        try:
                            session.refresh_credentials()
                        except Exception as ce:
                            bus.log("error",
                                    f"[{rt.vup}] 重建凭证失败: {ce}", rt.room_id)
                    else:
                        bus.log("error",
                                f"[{rt.vup}] getDanmuInfo 失败: {msg[:100]}，"
                                f"60s 后重试", rt.room_id)
                    token, hosts, host_idx = None, [], 0
                    retry += 1
                    rt.retries = retry
                    bus.emit("room_state", rt.snapshot())
                    await asyncio.sleep(60)
                    continue

            ws_url = hosts[host_idx % len(hosts)]
            connected_at = time.time()
            auth_failed = False
            state["close_requested"] = False
            exc: Exception | None = None
            rt.host_index = host_idx + 1
            try:
                async with websockets.connect(
                    ws_url, ping_interval=None,
                    origin="https://live.bilibili.com",
                    user_agent_header=None,
                    additional_headers={
                        "User-Agent": P.HEADERS["User-Agent"],
                        "Cookie": session.ws_cookie_header(),
                    },
                ) as ws:
                    await ws.send(P.pack(7, session.auth_payload(token)))
                    # 重连后兜底刷新开播状态，漏掉 LIVE 事件也能补落盘
                    if not rt.current_file:
                        try:
                            session.refresh_info()
                            rt.live_status = session.live_status
                            rt.title = session.title
                        except Exception:
                            pass
                        storage.ensure_out_file(session, rt, records_dir)
                    rt.connected = True
                    rt.connected_at = time.time()
                    bus.emit("room_state", rt.snapshot())
                    bus.log("info",
                            f"[{rt.vup}] WS 已连接"
                            f"（节点 {host_idx + 1}/{len(hosts)}）→ "
                            f"{rt.current_file or '（等待开播落盘）'}",
                            rt.room_id)

                    async def heartbeat() -> None:
                        while True:
                            await asyncio.sleep(P.HEARTBEAT_INTERVAL)
                            if state.get("close_requested"):
                                await ws.close()
                                return
                            await ws.send(P.pack(2))

                    async def pop_poll() -> None:
                        # 人气改走 HTTP（WS 心跳现网固定回 1）：连接后立即拉一次，
                        # 之后与心跳同节奏 25s；同步 httpx 调用放线程池避免阻塞事件循环
                        while True:
                            try:
                                info = await asyncio.to_thread(
                                    session.get_popularity)
                                _record_popularity(
                                    info, session, rt, bus, records_dir)
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                pass
                            for _ in range(P.HEARTBEAT_INTERVAL):
                                if state.get("close_requested"):
                                    return
                                await asyncio.sleep(1)

                    hb = asyncio.create_task(heartbeat())
                    pp = asyncio.create_task(pop_poll())
                    buf = b""
                    try:
                        async for raw in ws:
                            if isinstance(raw, str):
                                raw = raw.encode()
                            buf += raw
                            # 每帧重新合并房间覆盖：设置改动无需重连即生效
                            opts = _effective_opts(rt, settings)
                            while len(buf) >= 16:
                                plen, _, protover, op, _ = struct.unpack(
                                    ">IHHII", buf[:16])
                                if len(buf) < plen:
                                    break
                                payload = buf[16:plen]
                                buf = buf[plen:]
                                if protover == 2 and op == 5:
                                    try:
                                        payload = zlib.decompress(payload)
                                    except zlib.error:
                                        continue
                                    p = 0
                                    while p + 16 <= len(payload):
                                        sub_plen = struct.unpack(
                                            ">I", payload[p:p + 4])[0]
                                        sub_op = struct.unpack(
                                            ">I", payload[p + 8:p + 12])[0]
                                        sub = payload[p + 16:p + sub_plen]
                                        p += sub_plen
                                        _handle_message(
                                            sub_op, sub, session, rt, state,
                                            bus, records_dir, weak_dir,
                                            salt, opts)
                                elif op == 5:
                                    _handle_message(
                                        op, payload, session, rt, state,
                                        bus, records_dir, weak_dir, salt, opts)
                                elif op == 8:
                                    reply = payload.decode(errors="replace")
                                    if '"code":0' not in reply:
                                        bus.log("warning",
                                                f"[{rt.vup}] auth 被拒: "
                                                f"{reply[:120]}", rt.room_id)
                                        auth_failed = True
                                        break
                            if auth_failed or state.get("close_requested"):
                                break
                    finally:
                        hb.cancel()
                        pp.cancel()
            except (websockets.exceptions.ConnectionClosed, OSError) as e:
                exc = e
            except Exception as e:  # 握手异常等：记录后按断线重连处理
                exc = e
                bus.log("warning",
                        f"[{rt.vup}] 连接异常: {type(e).__name__}: {str(e)[:80]}",
                        rt.room_id)

            # ---- 统一重连决策 ----
            lifetime = time.time() - connected_at
            rt.connected = False
            if state.get("close_requested"):
                # 下播主动断线：不算风控失败，丢弃旧 token，回到轮询
                token, hosts, host_idx = None, [], 0
                retry, fast_fails = 0, 0
                rt.retries = 0
                rt.current_file = ""
                bus.emit("room_state", rt.snapshot())
                bus.log("info",
                        f"[{rt.vup}] 已断线释放连接，回到开播轮询"
                        f"（{poll_sec}s/次）", rt.room_id)
                continue
            host_idx += 1
            if host_idx >= len(hosts):
                token = None
            if auth_failed or lifetime < 15:
                retry += 1
                fast_fails += 1
                rt.retries = retry
                wait = min(10 * (2 ** min(retry - 1, 4)), 160) \
                    + random.randint(0, 5)
                code = getattr(exc, "code", "auth-reject" if auth_failed else "?")
                reason = getattr(exc, "reason", "") or ""
                bus.log("warning",
                        f"[{rt.vup}] 连接 {lifetime:.0f}s 即断"
                        f"（code={code} {str(reason)[:40]}），"
                        f"轮换节点，{wait}s 后重连", rt.room_id)
                bus.emit("room_state", rt.snapshot())
                await asyncio.sleep(wait)
            else:
                retry = 0
                fast_fails = 0
                rt.retries = 0
                bus.log("info",
                        f"[{rt.vup}] 连接断开（存活 {lifetime:.0f}s: "
                        f"{type(exc).__name__ if exc else '正常关闭'}），"
                        f"2s 后重连", rt.room_id)
                bus.emit("room_state", rt.snapshot())
                await asyncio.sleep(2)
    except asyncio.CancelledError:
        bus.log("info", f"[{rt.vup}] 录制任务已停止", rt.room_id)
        raise
    finally:
        # 先停视频协程（它仍在使用 session 的 httpx client），再关 session
        if video_task and not video_task.done():
            video_task.cancel()
            with contextlib.suppress(Exception):
                await video_task
        rt.running = False
        rt.connected = False
        rt.current_file = ""
        bus.emit("room_state", rt.snapshot())
        if session:
            session.close()


def _record_popularity(info: dict, session: LiveRoomSession, rt: RoomRuntime,
                       bus: EventBus, records_dir) -> None:
    """HTTP 轮询到人气后的统一处理：更新运行态、落盘、推送仪表盘。

    popularity 行可选带 watched（直播中的「累计看过」文案），供赛后战报使用。
    """
    pop = int(info.get("popularity") or 0)
    before = rt.popularity_now
    rt.update_popularity(pop)
    # 被脏值防护忽略（<=1）且此前无有效值时不产生噪声
    if rt.popularity_now == before and pop <= 1:
        return
    watched = str(info.get("watched") or "")
    if rt.current_file:
        rec = {"_type": "popularity", "ts": int(time.time()),
               "popularity": rt.popularity_now}
        if watched:
            rec["watched"] = watched
        try:
            with (records_dir / rt.vup / rt.current_file).open(
                    "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass
    bus.emit("popularity", {
        "room_id": rt.room_id, "vup": rt.vup,
        "popularity": rt.popularity_now, "peak": rt.popularity_peak})


def _handle_message(op: int, payload: bytes, session: LiveRoomSession,
                    rt: RoomRuntime, state: dict, bus: EventBus,
                    records_dir, weak_dir, salt: str, opts: dict) -> None:
    # op=3（心跳应答）现网固定回 1，不再含人气；人气改由 HTTP 轮询，见 pop_poll
    if op != 5:
        return
    try:
        msg = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    cmd = (msg.get("cmd") or "").split(":")[0]

    if cmd == "DANMU_MSG":
        info = msg.get("info", [])
        if len(info) < 3:
            return
        raw_text = info[1] or ""
        text = F.sanitize_text(raw_text) if opts["sanitize_text"] \
            else (raw_text or "").strip()
        if not text:
            return
        if opts["filter_pure_emoticon"] and F.is_pure_emoticon(text):
            rt.counters["emoticon_skipped"] = \
                rt.counters.get("emoticon_skipped", 0) + 1
            return
        user = info[2] if len(info) > 2 else []
        uid = user[0] if user else 0
        uname = user[1] if len(user) > 1 else ""
        medal = info[3] if len(info) > 3 else []
        mode = 1
        color = 0
        font_size = 0
        if isinstance(info[0], (list, tuple)) and len(info[0]) > 1:
            mode = info[0][1]
            # info[0]: [时间戳ms, 模式(1滚/4底/5顶), 字号, 颜色十进制 0xRRGGBB, ...]
            if len(info[0]) > 2:
                try:
                    font_size = int(info[0][2] or 0)
                except (TypeError, ValueError):
                    font_size = 0
            if len(info[0]) > 3:
                try:
                    color = int(info[0][3] or 0)
                except (TypeError, ValueError):
                    color = 0
        rec = {
            "_type": "danmaku",
            "text": text,
            "uid_anon": (F.anon_id(uid, salt) if uid and opts["anonymize"]
                         else (str(uid) if uid else "")),
            "uname_anon": (F.anon_name(uname) if opts["anonymize"] else uname),
            "medal": ({"name": medal[1], "level": medal[0]}
                      if len(medal) >= 2 else None),
            "ts": int(time.time()),
            "mode": mode,
        }
        if color > 0:
            rec["color"] = color      # 弹幕原色（ASS 渲染用；老文件无此键=白字）
        if font_size > 0:
            rec["fontsize"] = font_size
        state["recent"].append((uid, rec))
        out_file = storage.ensure_out_file(session, rt, records_dir)
        if out_file:
            storage.append_record(out_file, rec, rt, "danmaku")
            bus.emit("danmaku", {
                "kind": "danmaku", "room_id": rt.room_id, "vup": rt.vup,
                "text": text, "uname": rec["uname_anon"],
                "medal": rec["medal"], "ts": rec["ts"],
            })

    elif cmd == "SUPER_CHAT_MESSAGE":
        if not opts["record_sc"]:
            return
        d = msg.get("data", {})
        uid = d.get("uid", 0)
        uname = d.get("user_info", {}).get("uname", "")
        raw_msg = d.get("message", "")
        text = F.sanitize_text(raw_msg) if opts["sanitize_text"] \
            else (raw_msg or "").strip()
        rec = {
            "_type": "super_chat",
            "text": text,
            "uid_anon": (F.anon_id(uid, salt) if uid and opts["anonymize"]
                         else (str(uid) if uid else "")),
            "uname_anon": (F.anon_name(uname) if opts["anonymize"] else uname),
            "price_rmb": d.get("price", 0),
            "ts": int(time.time()),
        }
        # ASS SC 卡片渲染所需：停留秒数与 B 站下发的等级配色（#RRGGBB 字符串）；
        # 老文件无这些键时渲染走「金额内建等级色 + 10s」兜底
        try:
            sc_time = int(d.get("time") or 0)
        except (TypeError, ValueError):
            sc_time = 0
        if sc_time > 0:
            rec["sc_time"] = min(sc_time, 300)
        for _k, _field in (("sc_bg", "background_bottom_color"),
                           ("sc_fg", "content_color"),
                           ("sc_price_color", "background_price_color")):
            _v = d.get(_field)
            if isinstance(_v, str) and re.match(r"^#[0-9a-fA-F]{6}$", _v.strip()):
                rec[_k] = _v.strip().upper()
        out_file = storage.ensure_out_file(session, rt, records_dir)
        if out_file:
            storage.append_record(out_file, rec, rt, "super_chat")
            bus.emit("danmaku", {
                "kind": "super_chat", "room_id": rt.room_id, "vup": rt.vup,
                "text": text, "uname": rec["uname_anon"],
                "price": rec["price_rmb"], "ts": rec["ts"],
            })

    elif cmd == "SEND_GIFT_V2":
        if not opts["record_gifts"]:
            return
        d = msg.get("data", {})
        uid = d.get("uid", 0)
        gift_info = d.get("gift_info") if isinstance(d.get("gift_info"), dict) else {}
        coin_type = (d.get("coin_type")
                     or gift_info.get("coin_type", "")).lower()
        rec = {
            "_type": "gift",
            "uid_anon": (F.anon_id(uid, salt) if uid and opts["anonymize"]
                         else (str(uid) if uid else "")),
            "uname_anon": (F.anon_name(d.get("uname", ""))
                           if opts["anonymize"] else d.get("uname", "")),
            "gift_id": d.get("gift_id", 0),
            "gift_name": gift_info.get("name", "") or d.get("gift_name", ""),
            "num": d.get("num", 1),
            # 金瓜子计费：gold=付费(1000=¥1)，silver=免费礼物不计营收
            "coin_type": coin_type,
            "total_coin": d.get("total_coin", 0),
            "discount_price": d.get("discount_price", 0),
            "ts": int(time.time()),
        }
        out_file = storage.ensure_out_file(session, rt, records_dir)
        if out_file:
            storage.append_record(out_file, rec, rt, "gift")
            bus.emit("danmaku", {
                "kind": "gift", "room_id": rt.room_id, "vup": rt.vup,
                "text": f"赠送 {rec['gift_name'] or '礼物'} x{rec['num']}",
                "ts": rec["ts"],
            })

    elif cmd == "LIVE":
        rt.live_status = 1
        session.live_status = 1
        bus.log("info",
                f"[{rt.vup}] *** 开播事件 *** "
                f"{datetime.now().strftime('%H:%M:%S')}", rt.room_id)
        f = storage.ensure_out_file(session, rt, records_dir)
        if f:
            bus.emit("room_state", rt.snapshot())

    elif cmd == "PREPARING":
        rt.live_status = 0
        session.live_status = 0
        rt.current_file = ""
        state["close_requested"] = True  # 通知心跳关闭 WS，释放并发名额
        bus.log("info",
                f"[{rt.vup}] --- 下播事件，主动断线 --- "
                f"{datetime.now().strftime('%H:%M:%S')}", rt.room_id)

    elif cmd in ("ROOM_BLOCK_MSG", "ROOM_SILENT_USER_TO"):
        if opts["record_moderation"]:
            _handle_moderation(cmd, msg, session, rt, state, bus,
                               weak_dir, salt)


def _handle_moderation(cmd: str, msg: dict, session: LiveRoomSession,
                       rt: RoomRuntime, state: dict, bus: EventBus,
                       weak_dir, salt: str) -> None:
    """房管拉黑/禁言 → 回捞被处置者近 30 分钟弹幕 + 前 20 条语境。"""
    now = int(time.time())
    d = msg.get("data") or {}
    if not isinstance(d, dict):
        return
    uid = d.get("uid") or d.get("target_id") or 0
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return
    if not uid:
        return
    last = state["recent_events"].get(uid, 0)
    if now - last < EVENT_DEDUP_SEC:
        return
    state["recent_events"][uid] = now

    recent: deque = state["recent"]
    suspect, context = [], []
    for u, rec in recent:
        if now - rec.get("ts", 0) > RECENT_WINDOW_SEC:
            continue
        context.append(rec)
        if u == uid:
            suspect.append(rec)
    context = context[-CONTEXT_N:]

    raw = {k: v for k, v in d.items()
           if k not in ("uid", "uname", "face", "tail_icon", "uinfo")}
    event_name = "房管拉黑" if cmd == "ROOM_BLOCK_MSG" else "房管禁言"
    event_rec = {
        "_type": "moderation_event",
        "event": cmd,
        "event_name": event_name,
        "room_id": session.real_room_id,
        "vup": rt.vup,
        "uid_anon": F.anon_id(uid, salt),
        "uname_anon": F.anon_name(d.get("uname", "")),
        "ts": now,
        "suspect_danmaku": suspect,
        "context_before": context,
        "event_meta": raw,
        "weak_label_note": "房管处置弱标签，非金标准；suspect 可能为空（秒封/超窗）",
    }
    try:
        out_file = storage.append_moderation(weak_dir / rt.vup, rt.vup, event_rec)
    except OSError as e:
        bus.log("error", f"[{rt.vup}] 弱标签落盘失败: {e}", rt.room_id)
        return
    rt.bump("moderation")
    bus.log("warning",
            f"[{rt.vup}] !!! {event_name} "
            f"({event_rec['uid_anon'][:8]}) 回捞 {len(suspect)} 条 "
            f"→ {out_file.name}", rt.room_id)
    bus.emit("moderation", {
        "room_id": rt.room_id, "vup": rt.vup,
        "event_name": event_name, "suspect_count": len(suspect),
        "ts": now,
    })
