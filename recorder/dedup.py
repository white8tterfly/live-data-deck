"""录播文件的后处理去重。

规则：
- 仅 _type=danmaku 参与去重；session_header / super_chat / gift /
  moderation_event 原样保留
- 去重键 (uid, text)：同一用户相同文本在滑动窗口（默认 300s）内重复，
  只保留最早一条；window_sec=0 表示整个文件只留一条
- 命中关心名单的用户全部保留，输出记录追加 "cared": true
- 输出 *.dedup.jsonl，绝不覆盖原文件
"""
from __future__ import annotations

import json
from collections import OrderedDict
from pathlib import Path

from .filters import record_user_key

SEEN_MAX = 50000   # 单文件去重键上限，超限淘汰最旧


def dedup_output_path(src: Path) -> Path:
    return src.with_name(src.name.removesuffix(".jsonl") + ".dedup.jsonl")


def _process(src: Path, cared_keys: set[str], window_sec: int,
             write: bool) -> dict:
    seen: OrderedDict[tuple[str, str], int] = OrderedDict()
    # 刷屏组：key -> [uname, text, 总出现次数]
    groups: dict[tuple[str, str], list] = {}

    total = danmaku = kept = removed = cared = non_danmaku = 0
    out_lines: list[str] = []

    with src.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            total += 1
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                non_danmaku += 1  # 坏行原样保留
                if write:
                    out_lines.append(line)
                continue

            if rec.get("_type") != "danmaku":
                non_danmaku += 1
                if write:
                    out_lines.append(line)
                continue

            danmaku += 1
            uid = record_user_key(rec)
            text = rec.get("text", "")
            ts = int(rec.get("ts", 0))
            # 身份不明（uid 与用户名都缺失）的行不去重，直接保留
            if not uid:
                kept += 1
                if write:
                    out_lines.append(line)
                continue
            key = (uid, text)

            # 统计刷屏组（所有出现都计数，含跨窗口）
            grp = groups.get(key)
            if grp is None:
                grp = [rec.get("uname_anon", ""), text, 0]
                groups[key] = grp
            grp[2] += 1

            is_cared = bool(uid) and uid in cared_keys
            if is_cared:
                cared += 1
                kept += 1
                if write:
                    rec["cared"] = True
                    out_lines.append(json.dumps(rec, ensure_ascii=False))
                continue

            last_ts = seen.get(key)
            is_dup = last_ts is not None and (
                window_sec == 0 or ts - last_ts < max(1, window_sec))
            if is_dup:
                removed += 1
                continue

            seen[key] = ts
            seen.move_to_end(key)
            if len(seen) > SEEN_MAX:
                seen.popitem(last=False)
            kept += 1
            if write:
                out_lines.append(line)

    out_path = ""
    if write:
        dst = dedup_output_path(src)
        dst.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
        out_path = str(dst)

    top_spam = sorted(
        ({"uname": g[0], "text": g[1], "count": g[2]}
         for g in groups.values() if g[2] > 1),
        key=lambda x: x["count"], reverse=True)[:20]

    return {
        "src": str(src),
        "output": out_path,
        "window_sec": window_sec,
        "total_lines": total,
        "danmaku": danmaku,
        "kept": kept,
        "removed": removed,
        "cared_count": cared,
        "non_danmaku": non_danmaku,
        "removed_rate": round(removed / danmaku, 4) if danmaku else 0,
        "top_spam": top_spam,
    }


def preview_dedup(src: Path, cared_keys: set[str],
                  window_sec: int = 300) -> dict:
    return _process(src, cared_keys, window_sec, write=False)


def run_dedup(src: Path, cared_keys: set[str],
              window_sec: int = 300) -> dict:
    return _process(src, cared_keys, window_sec, write=True)
