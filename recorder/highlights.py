"""高能时刻检测：离线扫描场次 jsonl，输出 Top N 高能窗口。

三条独立信号（60s 桶）：
- 弹幕密度（条数，刷屏/爆梗）
- SC 连击（条数 + 金额，付费爆点）
- 人气斜率（25s 采样的桶内最大涨幅，名场面引流）

综合得分按各信号自身归一化加权，再做 120s 互斥抑制 + 相邻窗口合并；
每个窗口给出分段文件定位（part/偏移秒），供播放器跳转与 ffmpeg 秒切。
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

WINDOW_SEC = 60
TOP_N = 8
SUPPRESS_BUCKETS = 2          # 选中窗口两侧各 2 桶（120s）内抑制
MIN_DM = 10                  # 候选窗口最低弹幕量（SC/人气极强信号可豁免）

W_DM, W_SC, W_POP = 0.55, 0.28, 0.17


def _iter_jsonl(path: Path):
    """逐行读 jsonl，跳过空行与坏行。"""
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue


def _locate_part(meta: dict, ts: float) -> dict | None:
    """时间戳落在哪一个有效分段内，返回分段记录。"""
    for seg in meta.get("segments", []):
        if seg.get("discarded"):
            continue
        s, e = seg.get("start_ts", 0), seg.get("end_ts")
        if e is None:
            e = ts + 1  # 正在录制的末段
        if s - 1 <= ts <= e + 1:
            return seg
    return None


def detect_highlights(jsonl_path: Path, meta: dict,
                      window_sec: int = WINDOW_SEC,
                      top_n: int = TOP_N) -> dict:
    """扫描场次 jsonl，返回 Top N 高能窗口与整场时间轴。

    每条记录按 window_sec 落桶，桶内累计弹幕量 / 独立用户 / SC 条数与金额 /
    人气最大涨幅；三信号各自归一化后按 W_DM/W_SC/W_POP 加权成 score。
    候选需弹幕达标或 SC、人气出现极强单点；再按 score 降序挑选，
    相邻窗口互斥抑制 120s（SUPPRESS_BUCKETS），选中的连续桶合并成段。
    每个窗口附 part/偏移秒的定位信息，供播放器跳转与 ffmpeg 秒切。

    缺 start_ts 或 jsonl 不存在时抛错；时间窗内无数据则返回空结果 + note。
    """
    t0 = float(meta.get("start_ts") or 0)
    t1 = float(meta.get("end_ts") or 0) or 0
    if not t0:
        raise ValueError("场次 meta 缺少 start_ts")
    if not jsonl_path.exists():
        raise FileNotFoundError(f"弹幕记录不存在：{jsonl_path.name}")

    buckets: dict[int, dict] = {}

    def bucket(b: int) -> dict:
        if b not in buckets:
            buckets[b] = {"dm": 0, "users": set(), "sc": 0,
                          "sc_amount": 0.0, "slope": 0,
                          "phrases": Counter(), "samples": []}
        return buckets[b]

    pop_series: list[tuple[int, int]] = []  # (ts, popularity)
    for rec in _iter_jsonl(jsonl_path):
        typ = rec.get("_type")
        ts = rec.get("ts")
        if not ts:
            continue
        ts = float(ts)
        if ts < t0 - 1:
            continue
        if t1 and ts > t1 + 1:
            continue
        b = int(ts // window_sec) * window_sec
        if typ == "danmaku":
            d = bucket(b)
            d["dm"] += 1
            uid = rec.get("uid_anon") or ""
            if uid:
                d["users"].add(uid)
            text = (rec.get("text") or "").strip()
            if text:
                if len(d["samples"]) < 300:
                    d["samples"].append(
                        {"ts": int(ts), "text": text[:60],
                         "uname": rec.get("uname_anon") or ""})
                # 4~16 字的成句才参与刷屏口号统计
                if 2 <= len(text) <= 16:
                    d["phrases"][text] += 1
        elif typ == "super_chat":
            d = bucket(b)
            d["sc"] += 1
            try:
                d["sc_amount"] += float(rec.get("price_rmb") or 0)
            except (TypeError, ValueError):
                pass
            text = (rec.get("text") or "").strip()
            if text and len(d["samples"]) < 300:
                d["samples"].append(
                    {"ts": int(ts), "text": f"【SC ¥{rec.get('price_rmb', 0)}】"
                                            + text[:60],
                     "uname": rec.get("uname_anon") or ""})
        elif typ == "popularity":
            try:
                pop_series.append((int(ts), int(rec.get("popularity") or 0)))
            except (TypeError, ValueError):
                pass

    if not buckets:
        return {"base": meta.get("base"), "windows": [], "timeline": [],
                "note": "本场时间窗内没有弹幕/SC 数据，无法检测高能时刻"}

    # 人气斜率：落入同一桶的相邻采样最大正涨幅
    for (ts_a, p_a), (ts_b, p_b) in zip(pop_series, pop_series[1:]):
        if p_b > p_a:
            b = int(ts_b // window_sec) * window_sec
            if b in buckets:
                buckets[b]["slope"] = max(buckets[b]["slope"], p_b - p_a)

    bkeys = sorted(buckets)
    b0 = int(t0 // window_sec) * window_sec
    # 补齐空桶，时间轴连续
    for b in range(b0, bkeys[-1] + window_sec, window_sec):
        bucket(b)
    bkeys = sorted(buckets)

    max_dm = max(d["dm"] for d in buckets.values()) or 1
    max_sc = max(d["sc"] for d in buckets.values()) or 1
    max_slope = max(d["slope"] for d in buckets.values()) or 1
    max_amount = max(d["sc_amount"] for d in buckets.values()) or 1.0

    for b in bkeys:
        d = buckets[b]
        d["score"] = round(
            100 * (W_DM * d["dm"] / max_dm
                   + W_SC * (0.65 * d["sc"] / max_sc
                             + 0.35 * d["sc_amount"] / max_amount)
                   + W_POP * d["slope"] / max_slope), 1)

    # 候选：弹幕达标，或 SC/人气出现极强单点
    def is_candidate(b: int) -> bool:
        d = buckets[b]
        if d["dm"] >= MIN_DM:
            return True
        if d["sc"] >= 1 and d["score"] >= 25:
            return True
        if d["slope"] >= max_slope * 0.8 and d["slope"] > 0 and d["score"] >= 25:
            return True
        return False

    ranked = sorted((b for b in bkeys if is_candidate(b)),
                    key=lambda b: buckets[b]["score"], reverse=True)
    picked: list[int] = []
    for b in ranked:
        if all(abs(b - p) > SUPPRESS_BUCKETS * window_sec for p in picked):
            picked.append(b)
        if len(picked) >= top_n:
            break
    picked.sort()

    # 相邻（含间隔 1 桶内连续）的选中桶合并为高能段
    ranges: list[list[int]] = []
    for b in picked:
        if ranges and b - ranges[-1][-1] <= window_sec * (SUPPRESS_BUCKETS + 1):
            ranges[-1].append(b)
        else:
            ranges.append([b])

    windows = []
    for group in ranges:
        g_buckets = [b for b in range(group[0], group[-1] + window_sec, window_sec)]
        entries = [buckets[b] for b in g_buckets]
        best_b = max(g_buckets, key=lambda b: buckets[b]["score"])
        dm_sum = sum(e["dm"] for e in entries)
        sc_sum = sum(e["sc"] for e in entries)
        amount_sum = round(sum(e["sc_amount"] for e in entries), 1)
        slope_max = max(e["slope"] for e in entries)
        users_all = set()
        for e in entries:
            users_all |= e["users"]
        phrases = Counter()
        for e in entries:
            phrases.update(e["phrases"])
        top_phrases = [{"text": t, "count": c}
                       for t, c in phrases.most_common(5) if c >= 2]
        samples_all = []
        for e in entries:
            samples_all.extend(e["samples"])
        # 取覆盖时间内的 8 条代表弹幕：优先高频成句，再补时间中位附近的原句
        samples = []
        used = {p["text"] for p in top_phrases[:3]}
        for p in top_phrases[:3]:
            hit = next((s for s in samples_all if s["text"] == p["text"]), None)
            if hit:
                samples.append(hit)
        rest = sorted((s for s in samples_all if s["text"] not in used),
                      key=lambda s: s["ts"])
        samples.extend(rest[:8 - len(samples)])

        start_ts = float(group[0])
        end_ts = float(group[-1] + window_sec)
        tags = []
        if dm_sum >= MIN_DM and dm_sum / len(entries) >= max_dm * 0.55:
            tags.append("弹幕爆发")
        if sc_sum >= 1:
            tags.append("SC连击")
        if slope_max >= max_slope * 0.6 and slope_max > 0:
            tags.append("人气飙升")
        if not tags:
            tags.append("高能")

        part = _locate_part(meta, start_ts)
        part_file = part["file"] if part else None
        part_start = part["start_ts"] if part else start_ts
        part_end = part.get("end_ts") if part else None
        seek = None
        if part:
            clip_end = min(end_ts, part_end) if part_end else end_ts
            seek = {
                "part": part_file,
                "start_sec": round(start_ts - part_start, 1),
                "end_sec": round(max(0, clip_end - part_start), 1),
                "cross_part": part_end is not None and end_ts > part_end + 1,
            }
        windows.append({
            "start_ts": start_ts,
            "end_ts": end_ts,
            "start_clock": _hm(start_ts),
            "duration_sec": int(end_ts - start_ts),
            "score": buckets[best_b]["score"],
            "tags": tags,
            "danmaku": dm_sum,
            "super_chat": sc_sum,
            "sc_amount": amount_sum,
            "users": len(users_all),
            "pop_slope": slope_max,
            "top_phrases": top_phrases,
            "samples": samples,
            "seek": seek,
        })
    windows.sort(key=lambda w: w["score"], reverse=True)

    timeline = [{
        "ts": b,
        "dm": buckets[b]["dm"],
        "sc": buckets[b]["sc"],
        "slope": buckets[b]["slope"],
        "score": buckets[b]["score"],
    } for b in bkeys]

    return {
        "base": meta.get("base"),
        "start_ts": t0,
        "window_sec": window_sec,
        "windows": windows,
        "timeline": timeline,
        "totals": {
            "danmaku": sum(d["dm"] for d in buckets.values()),
            "super_chat": sum(d["sc"] for d in buckets.values()),
            "max_dm_per_window": max_dm,
            "max_slope": max_slope,
        },
    }


def _hm(ts: float) -> str:
    """时间戳 → HH:MM。"""
    import time as _t
    return _t.strftime("%H:%M", _t.localtime(ts))


# 直播心电图（Live ECG）聚合

ECG_BUCKET_SEC = 60
ECG_MAX_POINTS = 600


def build_ecg(jsonl_path: Path, meta: dict,
              bucket_sec: int = ECG_BUCKET_SEC,
              max_points: int = ECG_MAX_POINTS) -> dict:
    """把一场直播聚合成一条情绪波形带（多层信号），供前端 ECG 绘制。

    分层信号（同一时间桶）：
    - 弹幕密度 dm（连续折线/面积主轨迹）
    - SC 条数 sc + 金额合计 sc_amount（竖向金色尖峰，高度=金额）
    - 金礼物 gift 条数 + 营收 gift_amount（粒子点；银瓜子免费礼物不计）
    - 人气 pop（底层半透明面积；25s 采样，>1 才有效）

    桶数超过 max_points 时做**峰值保持下采样**（分组内弹幕/SC金额/礼物/人气取最大值，
    另附求和值供 tooltip），禁止只取均值把尖峰抹平。只读扫描，不落任何文件。
    缺 jsonl 时不抛异常，返回空序列 + missing 清单，供前端显式空态。
    """
    t0 = float(meta.get("start_ts") or 0)
    t1 = float(meta.get("end_ts") or 0)
    if not t0:
        raise ValueError("场次 meta 缺少 start_ts")

    buckets: dict[int, dict] = {}
    seen = {"danmaku": 0, "super_chat": 0, "gift": 0, "popularity": 0}
    last_ts = t0

    def bucket(b: int) -> dict:
        if b not in buckets:
            buckets[b] = {"dm": 0, "sc": 0, "sc_amount": 0.0,
                          "gift": 0, "gift_amount": 0.0, "pop": 0}
        return buckets[b]

    if jsonl_path.exists():
        for rec in _iter_jsonl(jsonl_path):
            typ = rec.get("_type")
            ts = rec.get("ts")
            if not ts:
                continue
            ts = float(ts)
            if ts < t0 - 1:
                continue
            if t1 and ts > t1 + 1:
                continue
            if ts > last_ts:
                last_ts = ts
            b = int(ts // bucket_sec) * bucket_sec
            if typ == "danmaku":
                bucket(b)["dm"] += 1
                seen["danmaku"] += 1
            elif typ == "super_chat":
                d = bucket(b)
                d["sc"] += 1
                try:
                    d["sc_amount"] += float(rec.get("price_rmb") or 0)
                except (TypeError, ValueError):
                    pass
                seen["super_chat"] += 1
            elif typ == "gift":
                if str(rec.get("coin_type") or "").lower() == "gold":
                    d = bucket(b)
                    d["gift"] += 1
                    try:
                        d["gift_amount"] += float(rec.get("total_coin") or 0) / 1000.0
                    except (TypeError, ValueError):
                        pass
                    seen["gift"] += 1
            elif typ == "popularity":
                try:
                    val = int(rec.get("popularity") or 0)
                except (TypeError, ValueError):
                    val = 0
                if val > 1:
                    d = bucket(b)
                    if val > d["pop"]:
                        d["pop"] = val
                    seen["popularity"] += 1

    end_ref = t1 or last_ts
    if jsonl_path.exists():
        b0 = int(t0 // bucket_sec) * bucket_sec
        # 末桶取「包含 end_ts 的分钟」：end_ts 恰在整分钟边界时不额外多出一个空桶
        b_end = max(b0, int((end_ref - 1) // bucket_sec) * bucket_sec)
        b_end = max(b_end, int(last_ts // bucket_sec) * bucket_sec)
        for b in range(b0, b_end + bucket_sec, bucket_sec):
            bucket(b)

    bkeys = sorted(buckets)
    series = [{
        "ts": b,
        "dm": buckets[b]["dm"],
        "dm_sum": buckets[b]["dm"],
        "sc": buckets[b]["sc"],
        "sc_amount": round(buckets[b]["sc_amount"], 1),
        "sc_amount_max": round(buckets[b]["sc_amount"], 1),
        "gift": buckets[b]["gift"],
        "gift_amount": round(buckets[b]["gift_amount"], 1),
        "pop": buckets[b]["pop"],
        "span": 1,
    } for b in bkeys]

    raw_points = len(series)
    group_sec = bucket_sec
    downsampled = False
    if max_points and raw_points > max_points:
        series, group_sec = _downsample_ecg(series, max_points, bucket_sec)
        downsampled = True

    missing = [k for k in ("danmaku", "super_chat", "gift", "popularity")
               if seen[k] == 0]
    dm_peak = max((p["dm"] for p in series), default=0)
    pop_peak = max((p["pop"] for p in series), default=0)
    note = ""
    if not jsonl_path.exists():
        note = "本场没有弹幕记录文件，无情绪波形可画（视频本身不受影响）"
    elif not any(seen.values()):
        note = "本场时间窗内没有弹幕 / SC / 礼物 / 人气数据"

    return {
        "base": meta.get("base"),
        "start_ts": t0,
        "end_ts": t1 or last_ts,
        "bucket_sec": bucket_sec,
        "group_sec": group_sec,
        "raw_points": raw_points,
        "downsampled": downsampled,
        "series": series,
        "missing": missing,
        "note": note,
        "totals": {
            "danmaku": seen["danmaku"],
            "super_chat": seen["super_chat"],
            "sc_amount": round(sum(p["sc_amount"] for p in series), 1),
            "gift": seen["gift"],
            "gift_amount": round(sum(p["gift_amount"] for p in series), 1),
            "dm_peak": dm_peak,
            "pop_peak": pop_peak,
        },
    }


def _downsample_ecg(series: list[dict], max_points: int,
                    bucket_sec: int) -> tuple[list[dict], int]:
    """峰值保持下采样：分组内弹幕/SC金额/礼物/人气取最大值，附求和与跨度。"""
    n = len(series)
    group = (n + max_points - 1) // max_points
    out: list[dict] = []
    for i in range(0, n, group):
        chunk = series[i:i + group]
        out.append({
            "ts": chunk[0]["ts"],
            "dm": max(c["dm"] for c in chunk),
            "dm_sum": sum(c["dm_sum"] for c in chunk),
            "sc": sum(c["sc"] for c in chunk),
            "sc_amount": round(sum(c["sc_amount"] for c in chunk), 1),
            "sc_amount_max": round(max(c["sc_amount_max"] for c in chunk), 1),
            "gift": sum(c["gift"] for c in chunk),
            "gift_amount": round(sum(c["gift_amount"] for c in chunk), 1),
            "pop": max(c["pop"] for c in chunk),
            "span": len(chunk),
        })
    return out, bucket_sec * group
