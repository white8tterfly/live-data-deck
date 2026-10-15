"""录制文件的多文件流式聚合分析（供「数据分析」页使用）。

只做只读统计，不修改任何文件。输出 JSON 友好的聚合结构，
柱状图由前端用纯 SVG 渲染，后端不依赖任何图表库。
"""
from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from pathlib import Path

from .filters import record_user_key

TIMELINE_BUCKETS = 48
PEAK_MINUTES = 5
PEAK_TOP_TEXTS = 20          # 每个高能分钟展开后展示的去重高频弹幕条数
PEAK_TOP_SC = 10             # 同一分钟内去重后的高频 SC 条数
TOP_N = 20

LEN_LABELS = ("1-5字", "6-10字", "11-20字", "21-50字", "50字+")
MODE_NAMES = {1: "滚动", 4: "底部", 5: "顶部"}


def _iter_records(path: Path):
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue


def _len_bucket(n: int) -> str:
    if n <= 5:
        return LEN_LABELS[0]
    if n <= 10:
        return LEN_LABELS[1]
    if n <= 20:
        return LEN_LABELS[2]
    if n <= 50:
        return LEN_LABELS[3]
    return LEN_LABELS[4]


def analyze_files(paths: list[Path]) -> dict:
    files_stat = []
    total_dm = total_sc = total_gift = total_mod = 0
    sc_amount = gift_amount = 0.0
    t_min, t_max = None, None

    users: dict[str, dict] = {}          # uid -> 聚合
    texts: Counter = Counter()
    medals: Counter = Counter()
    modes: Counter = Counter()
    lens: Counter = Counter()
    hourly: Counter = Counter()
    minutes: Counter = Counter()
    minute_users: dict[int, set] = defaultdict(set)

    for p in paths:
        f_dm = f_sc = f_gift = f_mod = 0
        f_users: set[str] = set()
        f_t0 = f_t1 = None
        for rec in _iter_records(p):
            typ = rec.get("_type")
            if typ == "danmaku":
                f_dm += 1
                total_dm += 1
                uid = record_user_key(rec)
                uname = rec.get("uname_anon", "")
                text = rec.get("text", "")
                ts = int(rec.get("ts", 0)) or None
                if ts:
                    t_min = ts if t_min is None else min(t_min, ts)
                    t_max = ts if t_max is None else max(t_max, ts)
                    f_t0 = ts if f_t0 is None else min(f_t0, ts)
                    f_t1 = ts if f_t1 is None else max(f_t1, ts)
                    hourly[time.localtime(ts).tm_hour] += 1
                    m = ts - ts % 60
                    minutes[m] += 1
                    if uid:
                        minute_users[m].add(uid)
                if uid:
                    f_users.add(uid)
                    u = users.setdefault(uid, {"uid": uid, "uname": uname,
                                               "count": 0, "sc_count": 0,
                                               "sc_amount": 0.0})
                    u["count"] += 1
                    if uname:
                        u["uname"] = uname
                if text:
                    texts[text] += 1
                    lens[_len_bucket(len(text))] += 1
                medal = rec.get("medal")
                if isinstance(medal, dict) and medal.get("name"):
                    medals[medal["name"]] += 1
                modes[MODE_NAMES.get(rec.get("mode"), "其他")] += 1
            elif typ == "super_chat":
                f_sc += 1
                total_sc += 1
                price = float(rec.get("price_rmb", 0) or 0)
                sc_amount += price
                uid = record_user_key(rec)
                if uid:
                    u = users.setdefault(uid, {"uid": uid,
                                               "uname": rec.get("uname_anon", ""),
                                               "count": 0, "sc_count": 0,
                                               "sc_amount": 0.0})
                    u["sc_count"] += 1
                    u["sc_amount"] += price
                    if rec.get("uname_anon"):
                        u["uname"] = rec["uname_anon"]
            elif typ == "gift":
                f_gift += 1
                total_gift += 1
                if str(rec.get("coin_type", "")).lower() == "gold":
                    gift_amount += float(rec.get("total_coin", 0) or 0) / 1000
            elif typ == "moderation_event":
                f_mod += 1
                total_mod += 1
        files_stat.append({
            "vup": p.parent.name,
            "name": p.name,
            "danmaku": f_dm, "super_chat": f_sc, "gift": f_gift,
            "moderation": f_mod, "users": len(f_users),
            "start": f_t0, "end": f_t1,
        })

    # ---- 时间轴 48 桶 ----
    timeline = []
    if t_min and t_max and t_max > t_min:
        span = t_max - t_min
        bucket = max(1, span // TIMELINE_BUCKETS + 1)
        raw = [0] * (span // bucket + 1)
        for m, c in minutes.items():
            idx = min(len(raw) - 1, (m - m % bucket - t_min) // bucket)
            raw[idx] += c
        peak_idx = max(range(len(raw)), key=lambda i: raw[i]) if raw else 0
        for i, v in enumerate(raw):
            bt = t_min + i * bucket
            timeline.append({
                "t0": bt,
                "label": time.strftime("%H:%M", time.localtime(bt)),
                "value": v,
            })
    elif t_min:
        timeline = [{"t0": t_min,
                     "label": time.strftime("%H:%M", time.localtime(t_min)),
                     "value": total_dm}]
        peak_idx = 0
    else:
        peak_idx = 0

    # ---- 高能分钟 Top 5（二次扫描：按内容去重统计高频弹幕 / SC） ----
    peak_mins = sorted(minutes.items(), key=lambda kv: kv[1],
                       reverse=True)[:PEAK_MINUTES]
    peak_set = {m for m, _ in peak_mins}
    minute_texts: dict[int, Counter] = defaultdict(Counter)
    minute_sc: dict[int, Counter] = defaultdict(Counter)
    minute_sc_price: dict[int, dict[str, float]] = defaultdict(dict)
    for p in paths:
        for rec in _iter_records(p):
            ts = int(rec.get("ts", 0) or 0)
            if not ts:
                continue
            m = ts - ts % 60
            if m not in peak_set:
                continue
            typ = rec.get("_type")
            if typ == "danmaku":
                text = (rec.get("text") or "").strip()
                if text:
                    minute_texts[m][text[:80]] += 1
            elif typ == "super_chat":
                text = (rec.get("text") or "").strip() or "（无留言 SC）"
                key = text[:80]
                minute_sc[m][key] += 1
                try:
                    minute_sc_price[m][key] = float(rec.get("price_rmb") or 0)
                except (TypeError, ValueError):
                    minute_sc_price[m].setdefault(key, 0.0)
    peak_minutes = []
    for m, c in peak_mins:
        sc_counter = minute_sc.get(m, Counter())
        sc_prices = minute_sc_price.get(m, {})
        top_texts = [{"text": t, "count": n}
                     for t, n in minute_texts.get(m, Counter())
                     .most_common(PEAK_TOP_TEXTS)]
        top_sc = []
        for t, n in sc_counter.most_common(PEAK_TOP_SC):
            price = round(sc_prices.get(t, 0.0), 2)
            top_sc.append({"text": t, "count": n, "price": price,
                           "amount": round(price * n, 2)})
        peak_minutes.append({
            "ts": m,
            "label": time.strftime("%m-%d %H:%M", time.localtime(m)),
            "count": c,
            "users": len(minute_users.get(m, ())),
            "sc_count": sum(sc_counter.values()),
            "sc_amount": round(
                sum(sc_prices.get(t, 0.0) * n for t, n in sc_counter.items()),
                2),
            "top_texts": top_texts,
            "top_sc": top_sc,
        })

    span_sec = (t_max - t_min) if t_min and t_max else 0
    avg_per_min = round(total_dm / span_sec * 60, 1) if span_sec else 0

    return {
        "file_count": len(paths),
        "files": files_stat,
        "totals": {
            "danmaku": total_dm,
            "users": len(users),
            "super_chat": total_sc,
            "sc_amount": round(sc_amount, 2),
            "gift": total_gift,
            "gift_amount": round(gift_amount, 2),
            "revenue": round(sc_amount + gift_amount, 2),
            "moderation": total_mod,
            "start": t_min, "end": t_max, "span_sec": span_sec,
            "peak_per_min": max(minutes.values()) if minutes else 0,
            "avg_per_min": avg_per_min,
        },
        "per_file_bars": [
            {"label": f"{f['vup']}/{f['name'].replace('live_','').replace('.jsonl','')}",
             "value": f["danmaku"]} for f in files_stat],
        "timeline": {"bars": timeline, "peak_index": peak_idx},
        "hourly": [hourly.get(h, 0) for h in range(24)],
        "peak_minutes": peak_minutes,
        "top_texts": [{"text": t, "count": c}
                      for t, c in texts.most_common(TOP_N)],
        "top_users": sorted(
            ({"uid": u["uid"], "uname": u["uname"], "count": u["count"],
              "sc_count": u["sc_count"], "sc_amount": round(u["sc_amount"], 2)}
             for u in users.values()),
            key=lambda x: x["count"], reverse=True)[:TOP_N],
        "length_buckets": [{"label": l, "value": lens.get(l, 0)} for l in LEN_LABELS],
        "sc_users": sorted(
            ({"uid": u["uid"], "uname": u["uname"],
              "count": u["sc_count"], "amount": round(u["sc_amount"], 2)}
             for u in users.values() if u["sc_count"] > 0),
            key=lambda x: x["amount"], reverse=True)[:10],
        "top_medals": [{"name": n, "count": c}
                       for n, c in medals.most_common(10)],
        "modes": [{"label": m, "value": modes.get(m, 0)}
                  for m in ("滚动", "底部", "顶部", "其他") if modes.get(m)],
    }
