"""新手引导的临时演示数据：造一份能被「录制文件」「数据分析」「视频录制」直接读取的场次产物。

教程走到这几个页面时磁盘上还没有任何录制产物，页面会是空的；这里按真实录制落下的
行结构造出弹幕记录、房管处置弱标签与一份视频场次 meta 把页面撑起来，教程一结束
（或服务重启）就整目录删掉。
字段严格跟随 storage / room_task / video_recorder 里真实写出的内容，只把身份换成本地
生成的匿名 uid 与「观众A」这类一眼可辨的演示用户名；视频分段用占位 flv 凑体积，
不保证可播放。三个演示目录名固定为 DEMO_VUP，与真实主播目录不冲突，便于整目录清理。
"""
from __future__ import annotations

import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import VIDEO_QN_LABELS
from .filters import anon_id

# 演示用的主播目录名，与真实 vup 目录互不冲突，便于整目录删除
DEMO_VUP = "__onboarding_demo__"
# 匿名 uid 的固定盐：数字变 16 位十六进制，和真实匿名化后的 uid_anon 同形
_DEMO_SALT = "onboarding-demo"
# 演示事件以「当前时间往前 20 分钟」为起点铺开
_SPAN_BACK_SEC = 1200
# 视频场次写死时长（秒）。写死 end_ts 可避开 /api/videos 的「异常退出认回残留分段」分支
_DEMO_VIDEO_SEC = 1800
# 占位分段体积，仅凑出正常的大小 / 分段数观感
_DEMO_FLV_BYTES = 20480
# 弱标签回捞窗口与上下文条数，对齐 room_task 的 RECENT_WINDOW_SEC / CONTEXT_N
_WEAK_WINDOW_SEC = 1800
_WEAK_CONTEXT_N = 20


def _demo_dir(base_dir) -> Path:
    """演示目录：base_dir / DEMO_VUP。

    路径由服务端配置派生，resolve 后再确认仍落在 base_dir 内，
    越界直接抛 ValueError（常量里本没有分隔符，这道校验是兜底）。
    """
    base = Path(base_dir).resolve()
    d = (base / DEMO_VUP).resolve()
    try:
        d.relative_to(base)
    except ValueError:
        raise ValueError("演示目录越界")
    return d


def _header() -> dict:
    """场次头，字段对齐 storage.ensure_out_file 写出的 session_header。"""
    return {
        "_type": "session_header",
        "room_id": 0,
        "vup": DEMO_VUP,
        "anchor_name": "演示主播",
        "title": "新手引导演示场次",
        "live_status": 1,
        "cover_url": "",
        "live_start": "",
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }


_USERS = ("观众A", "观众B", "观众C", "观众D", "观众E", "观众F")


def _uid(idx: int) -> str:
    """第 idx 位演示用户的匿名 uid，与真实匿名化后的 16 位十六进制同形。"""
    return anon_id(1000 + idx, _DEMO_SALT)


def _build_events(t0: int) -> list[dict]:
    """造 24 条事件：18 弹幕 + 2 SC + 1 礼物 + 3 人气，按时间先后排好。

    t0 为场次起点 unix 秒，每条记录的 ts = t0 + 相对秒数。
    """
    def dm(sec, idx, text, mode=1, medal=None, color=0, fontsize=0):
        rec = {"_type": "danmaku", "text": text, "uid_anon": _uid(idx),
               "uname_anon": _USERS[idx], "medal": medal,
               "ts": t0 + sec, "mode": mode}
        if color:
            rec["color"] = color
        if fontsize:
            rec["fontsize"] = fontsize
        return rec

    def sc(sec, idx, text, price, sc_time=0):
        rec = {"_type": "super_chat", "text": text, "uid_anon": _uid(idx),
               "uname_anon": _USERS[idx], "price_rmb": price, "ts": t0 + sec}
        if sc_time:
            rec["sc_time"] = sc_time
        return rec

    def gift(sec, idx, name, num, total_coin):
        return {"_type": "gift", "uid_anon": _uid(idx),
                "uname_anon": _USERS[idx], "gift_id": 31036,
                "gift_name": name, "num": num, "coin_type": "gold",
                "total_coin": total_coin, "discount_price": 0,
                "ts": t0 + sec}

    def pop(sec, value, watched=""):
        rec = {"_type": "popularity", "ts": t0 + sec, "popularity": value}
        if watched:
            rec["watched"] = watched
        return rec

    return [
        dm(0, 0, "刚开播就来啦，主播今天唱什么"),
        dm(20, 1, "晚上好，蹲一个点歌"),
        pop(45, 1200, "看过 1.1 万"),
        dm(60, 2, "哈哈哈这段太好笑了", color=16711680),
        dm(95, 3, "弹幕护体，前排占座"),
        sc(130, 1, "第一次发 SC，祝直播顺利", 30, sc_time=60),
        dm(160, 0, "主播的声音越来越稳了", medal={"name": "观众团", "level": 12}),
        dm(195, 4, "等着听下一首，别停"),
        pop(230, 2600, "看过 1.3 万"),
        dm(260, 3, "这首我单曲循环好久了", fontsize=25),
        gift(300, 5, "小心心", 10, 1000),
        dm(330, 5, "高音稳，好好听", medal={"name": "观众团", "level": 8}),
        dm(370, 2, "已关注，每天都来看", medal={"name": "观众团", "level": 3}),
        dm(410, 3, "下班路上就等着这场直播"),
        pop(450, 3100, "看过 1.4 万"),
        dm(500, 0, "求一个歌单，回头慢慢听"),
        dm(560, 4, "今晚氛围真好"),
        sc(620, 3, "点一首歌，随便唱都行", 50),
        dm(700, 1, "刚到家，还好没错过"),
        dm(780, 5, "笑死，这个梗我记下了"),
        dm(860, 2, "主播加油，小礼物送上"),
        dm(950, 3, "这段可以剪出来循环"),
        dm(1050, 0, "下次直播是什么时候呀"),
        dm(1150, 4, "辛苦啦，记得早点休息"),
    ]


def _build_weak_events(t0: int, events: list[dict]) -> list[dict]:
    """按真实 moderation_event 行结构造 2 条房管处置弱标签。

    每条 (相对秒, 被处置用户, 明文字号, 命令, 中文名, 备注)：
    suspect / context 都取自演示弹幕里真实存在的行，与线上 _handle_moderation
    回捞近窗弹幕的做法一致（只截断到处置时刻之前、只算弹幕、上下文取末 20 条）。
    """
    danmaku = [r for r in events if r.get("_type") == "danmaku"]
    specs = (
        (520, "观众D", 1003, "ROOM_BLOCK_MSG", "房管拉黑", "这条被房管拉黑了"),
        (1180, "观众F", 1005, "ROOM_SILENT_USER_TO", "房管禁言", "疑似刷屏，先禁言"),
    )
    out = []
    for sec, name, uid_int, cmd, event_name, note in specs:
        ts = t0 + sec
        window = [r for r in danmaku
                  if ts - _WEAK_WINDOW_SEC <= r.get("ts", 0) <= ts]
        suspect = [r for r in window if r.get("uname_anon") == name]
        out.append({
            "_type": "moderation_event",
            "event": cmd,
            "event_name": event_name,
            "room_id": 0,
            "vup": DEMO_VUP,
            "uid_anon": anon_id(uid_int, _DEMO_SALT),
            "uname_anon": name,
            "ts": ts,
            "suspect_danmaku": suspect,
            "context_before": window[-_WEAK_CONTEXT_N:],
            "event_meta": {"msg": note, "msg_type": 1,
                           "target_id": uid_int, "is_admin": 1},
            "weak_label_note": "房管处置弱标签，非金标准；suspect 可能为空（秒封/超窗）",
        })
    return out


def _build_video_meta(base: str, jsonl_name: str, t0: int) -> dict:
    """造一份真实形状的 video_session meta。

    start_ts 与演示弹幕同起点，好让高能检测的时间窗内有数据；
    end_ts 写死为 start_ts + _DEMO_VIDEO_SEC，避免 /api/videos 因缺 end_ts 走
    「异常退出认回残留分段」分支（把占位 flv 标成中断恢复、时长算成「现在-开播」）。
    """
    start_ts = float(t0)
    end_ts = start_ts + _DEMO_VIDEO_SEC
    seg_file = f"{base}.flv"
    return {
        "_type": "video_session",
        "version": 1,
        "room_id": 0,
        "vup": DEMO_VUP,
        "anchor_name": "演示主播",
        "title": "新手引导演示场次",
        "base": base,
        "start_ts": start_ts,
        "start_clock": datetime.fromtimestamp(start_ts)
            .strftime("%Y-%m-%d %H:%M:%S"),
        "end_ts": end_ts,
        "end_reason": "live_end",
        "qn": 10000,
        "qn_label": VIDEO_QN_LABELS.get(10000, "原画"),
        "requested_qn": 10000,
        "protocol": "http_stream",
        "format": "flv",
        "codec": "avc",
        "jsonl": jsonl_name,
        "segments": [{
            "file": seg_file,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "bytes": _DEMO_FLV_BYTES,
            "node": "",
            "result": "ok",
            "detail": "",
            "discarded": False,
        }],
        "ass": None,
    }


def write_demo(records_dir, weak_dir, video_dir) -> dict:
    """生成演示产物：弹幕记录 + 房管弱标签 + 一份视频场次。

    重复调用直接重建（先删目录再写），不报错。返回
    {"vup", "file", "records", "weak_file", "video_base", "video_segments"}；
    records 只数事件行，不含首行 session_header。
    三个目录均由入参派生并校验在各自根目录内，不接受任何前端路径。
    """
    t0 = int(time.time()) - _SPAN_BACK_SEC
    stamp = datetime.now()

    # 弹幕记录
    rec_d = _demo_dir(records_dir)
    if rec_d.exists():
        shutil.rmtree(rec_d)
    rec_d.mkdir(parents=True, exist_ok=True)
    filename = f"live_{stamp.strftime('%Y%m%d_%H%M')}.jsonl"
    events = _build_events(t0)
    with (rec_d / filename).open("w", encoding="utf-8") as f:
        for rec in [_header(), *events]:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # 房管处置弱标签
    weak_d = _demo_dir(weak_dir)
    if weak_d.exists():
        shutil.rmtree(weak_d)
    weak_d.mkdir(parents=True, exist_ok=True)
    weak_name = f"weak_{stamp.strftime('%Y%m%d')}.jsonl"
    with (weak_d / weak_name).open("w", encoding="utf-8") as f:
        for rec in _build_weak_events(t0, events):
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # 视频场次：meta + 与分段同名的占位 flv
    video_d = _demo_dir(video_dir)
    if video_d.exists():
        shutil.rmtree(video_d)
    video_d.mkdir(parents=True, exist_ok=True)
    base = f"live_{stamp.strftime('%Y%m%d_%H%M')}"
    meta = _build_video_meta(base, filename, t0)
    (video_d / f"{base}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (video_d / meta["segments"][0]["file"]).write_bytes(
        b"\x00" * _DEMO_FLV_BYTES)

    return {"vup": DEMO_VUP, "file": filename, "records": len(events),
            "weak_file": weak_name, "video_base": base, "video_segments": 1}


def cleanup_demo(records_dir, weak_dir, video_dir) -> bool:
    """删除三个演示目录，返回是否有任何一个被真删掉；都不存在时返回 False。"""
    removed = False
    for base_dir in (records_dir, weak_dir, video_dir):
        d = _demo_dir(base_dir)
        if d.exists():
            shutil.rmtree(d)
            removed = True
    return removed
