"""单场直播战报：jsonl 聚合 + Pillow 一页 PNG 长图渲染。

数据口径：
- 人气值（popularity）来自 WS op=3 心跳应答（25s 粒度），是 B 站平台加权
  指标，非真实独立人数；旧版录制文件没有该字段，曲线区显示缺数据提示
- 营收 = SC 金额(price_rmb) + 金礼物（total_coin/1000），银瓜子免费礼物不计
"""
from __future__ import annotations

import io
import json
import time
from collections import Counter
from pathlib import Path

from .filters import record_user_key

# ---------- 配色（与 Web 深色主题一致） ----------
BG = (13, 15, 21)
PANEL = (23, 27, 38)
PANEL2 = (29, 34, 48)
BORDER = (39, 46, 64)
TEXT = (233, 236, 243)
TEXT2 = (166, 174, 194)
TEXT3 = (107, 116, 136)
PRIMARY = (251, 114, 153)
GOLD = (246, 185, 59)
BLUE = (91, 157, 255)
GREEN = (57, 213, 155)
RED = (255, 92, 124)

W = 1080
MARGIN = 36

_FONT_CACHE: dict = {}


def _font(size: int, bold: bool = False):
    from PIL import ImageFont
    key = (size, bold)
    if key not in _FONT_CACHE:
        path = "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc"
        try:
            _FONT_CACHE[key] = ImageFont.truetype(path, size)
        except OSError:
            _FONT_CACHE[key] = ImageFont.load_default()
    return _FONT_CACHE[key]


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


def summarize_file(path: Path) -> dict:
    """离线聚合单个录制文件。"""
    header = {}
    danmaku = users_n = sc_n = gift_n = 0
    sc_amount = gift_amount = 0.0
    t0 = t1 = None
    user_set: set[str] = set()
    texts: Counter = Counter()
    medals: Counter = Counter()
    sc_users: dict[str, dict] = {}
    minutes: Counter = Counter()
    pop_series: list[list[int]] = []
    watched_recorded = ""  # 录制时随人气采样落盘的「累计看过」最后值

    for rec in _iter_records(path):
        typ = rec.get("_type")
        if typ == "session_header" and not header:
            header = rec
        elif typ == "danmaku":
            danmaku += 1
            uid = record_user_key(rec)
            if uid:
                user_set.add(uid)
            ts = int(rec.get("ts", 0) or 0)
            if ts:
                t0 = ts if t0 is None else min(t0, ts)
                t1 = ts if t1 is None else max(t1, ts)
                minutes[ts - ts % 60] += 1
            if rec.get("text"):
                texts[rec["text"]] += 1
            medal = rec.get("medal")
            if isinstance(medal, dict) and medal.get("name"):
                medals[medal["name"]] += 1
        elif typ == "super_chat":
            sc_n += 1
            sc_amount += float(rec.get("price_rmb", 0) or 0)
            ts = int(rec.get("ts", 0) or 0)
            if ts:
                t0 = ts if t0 is None else min(t0, ts)
                t1 = ts if t1 is None else max(t1, ts)
            uid = record_user_key(rec)
            if uid:
                u = sc_users.setdefault(uid, {"uname": rec.get("uname_anon", ""),
                                              "count": 0, "amount": 0.0})
                u["count"] += 1
                u["amount"] += float(rec.get("price_rmb", 0) or 0)
        elif typ == "gift":
            gift_n += 1
            if str(rec.get("coin_type", "")).lower() == "gold":
                gift_amount += float(rec.get("total_coin", 0) or 0) / 1000
        elif typ == "popularity":
            ts = int(rec.get("ts", 0) or 0)
            val = int(rec.get("popularity", 0) or 0)
            # val<=1 为无效采样（早期版本 WS 心跳在现网固定回 1 的脏数据）
            if ts and val > 1:
                pop_series.append([ts, val])
            w = str(rec.get("watched") or "").strip()
            if w:
                watched_recorded = w

    pop_series.sort(key=lambda p: p[0])
    pop_vals = [v for _, v in pop_series]
    peak_minute = max(minutes.items(), key=lambda kv: kv[1]) if minutes else (0, 0)
    span = (t1 - t0) if t0 and t1 else 0
    date_str = ""
    if t0:
        date_str = time.strftime("%Y-%m-%d", time.localtime(t0))
    elif header.get("collected_at"):
        date_str = header["collected_at"][:10]

    # 弹幕分钟桶（始终 60s，与"弹幕峰值"卡片口径一致；长直播柱密但不合并）
    dm_bars = []
    dm_bucket = 60
    if t0 and t1 and span > 0:
        n = span // 60 + 1
        raw = [0] * n
        for m, c in minutes.items():
            idx = min(n - 1, (m - t0) // 60)
            raw[idx] += c
        dm_bars = raw

    return {
        "vup": header.get("vup") or path.parent.name,
        "anchor": header.get("anchor_name", ""),
        "title": header.get("title", ""),
        "room_id": header.get("room_id", 0),
        "cover_url": header.get("cover_url", ""),
        "live_start": header.get("live_start", ""),
        "date": date_str,
        "start_ts": t0, "end_ts": t1, "span_sec": span,
        "danmaku": danmaku,
        "users": len(user_set),
        "sc_count": sc_n,
        "sc_amount": round(sc_amount, 2),
        "gift_count": gift_n,
        "gift_amount": round(gift_amount, 2),
        "revenue": round(sc_amount + gift_amount, 2),
        "pop_series": pop_series,
        "pop_peak": max(pop_vals) if pop_vals else 0,
        "pop_avg": round(sum(pop_vals) / len(pop_vals)) if pop_vals else 0,
        "has_pop": bool(pop_series),
        "watched_show": "",
        "watched_recorded": watched_recorded,
        "dm_peak": {"ts": peak_minute[0], "count": peak_minute[1]},
        "dm_bars": dm_bars,
        "dm_bucket": dm_bucket,
        "top_texts": texts.most_common(10),
        "top_sc": sorted(sc_users.values(),
                         key=lambda u: u["amount"], reverse=True)[:5],
        "top_medals": medals.most_common(5),
    }


def enrich_online(s: dict, sessdata: str = "") -> dict:
    """尽力而为补：封面（旧文件没有）与「累计看过」文案。失败静默。

    注意（2026-10 实测）：
    - getInfoByRoom 必须带浏览器指纹 cookie + wbi 签名，否则返回 -352；
    - 「累计看过」在顶层 watched_show.text_large（旧版是 watched.show，做兼容）；
    - 该字段直播中为真实累计（如「5798人看过」），下播后会被重置成个位数的无意义值，
      因此只在 room_info.live_status==1 时采用；
    - 在线接口只能拿到房间「当前」状态，无法还原某场历史直播，旧文件补不到。
    """
    s.setdefault("live_status_now", None)
    try:
        import httpx
        from . import protocol as P
        room = s.get("room_id")
        if not room:
            return s
        cookies = {"SESSDATA": sessdata} if sessdata else None
        with httpx.Client(headers=P.HEADERS, cookies=cookies, timeout=12,
                          follow_redirects=True) as cli:
            P.init_risk_cookies(cli)
            if not s.get("cover_url"):
                info = cli.get(
                    "https://api.live.bilibili.com/room/v1/Room/get_info",
                    params={"room_id": room}).json().get("data", {})
                s["cover_url"] = (info.get("keyframe", "")
                                  or info.get("user_cover", ""))
            try:
                wbi_key = P.get_wbi_key(cli)
                params = P.wbi_sign({"room_id": room}, wbi_key)
                body = cli.get(
                    "https://api.live.bilibili.com/xlive/web-room/v1/index/getInfoByRoom",
                    params=params).json()
                if body.get("code") == 0:
                    d = body.get("data", {})
                    live_status = (d.get("room_info", {}) or {}).get("live_status")
                    s["live_status_now"] = live_status
                    # 仅直播中采用：下播后 watched_show 会被重置为个位数无意义值
                    if live_status == 1:
                        ws = d.get("watched_show") or d.get("watched") or {}
                        s["watched_show"] = ws.get("text_large") or ws.get("show") or ""
                    else:
                        s["watched_show"] = ""
            except Exception:
                pass
    except Exception:
        pass
    return s


# ================= PNG 渲染 =================

def _text_w(draw, txt, font) -> int:
    return int(draw.textlength(str(txt), font=font))


def _wrap(draw, txt, font, max_w):
    lines, cur = [], ""
    for ch in str(txt):
        if ch == "\n":
            lines.append(cur); cur = ""; continue
        if _text_w(draw, cur + ch, font) <= max_w:
            cur += ch
        else:
            lines.append(cur); cur = ch
    if cur:
        lines.append(cur)
    return lines[:2]  # 标题最多两行


def _panel(draw, img, x, y, w, h, r=16):
    draw.rounded_rectangle([x, y, x + w, y + h], radius=r,
                           fill=PANEL, outline=BORDER, width=1)


def _fmt_n(v) -> str:
    return f"{int(v):,}"


def _fmt_dur(sec) -> str:
    sec = int(sec or 0)
    h, m = sec // 3600, (sec % 3600) // 60
    if h:
        return f"{h}小时{m}分"
    return f"{m}分钟" if m else f"{sec}秒"


def _download_cover(url: str):
    if not url:
        return None
    try:
        import httpx
        from PIL import Image
        r = httpx.get(url, timeout=10, follow_redirects=True, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://live.bilibili.com/",
        })
        if r.status_code == 200:
            return Image.open(io.BytesIO(r.content)).convert("RGB")
    except Exception:
        return None
    return None


def _cover_round(img_src, w, h):
    """封面等比裁剪 + 圆角。"""
    from PIL import Image, ImageDraw
    img_src = img_src.copy()
    sw, sh = img_src.size
    scale = max(w / sw, h / sh)
    nw, nh = max(w, int(sw * scale) + 1), max(h, int(sh * scale) + 1)
    img_src = img_src.resize((nw, nh))
    img_src = img_src.crop(((nw - w) // 2, (nh - h) // 2,
                            (nw + w) // 2, (nh + h) // 2))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w, h], radius=14, fill=255)
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.paste(img_src, (0, 0), mask)
    return out


def _cover_placeholder(w, h):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (w, h), PANEL2)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=14,
                        outline=BORDER, width=1)
    f = _font(18)
    msg = "暂无封面"
    d.text((w / 2 - _text_w(d, msg, f) / 2, h / 2 - 12), msg,
           font=f, fill=TEXT3)
    return img


def render_png(s: dict, out_path: Path | None = None) -> bytes:
    from PIL import Image, ImageDraw

    H = 2050  # 画布预留，末尾按实际内容裁剪
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    f_brand = _font(15)
    f_h2 = _font(20, bold=True)
    f_card_v = _font(30, bold=True)
    f_card_l = _font(14)
    f_body = _font(16)
    f_small = _font(13)
    f_mono = _font(15)

    content_w = W - MARGIN * 2
    y = MARGIN

    # ---------- 头部 ----------
    cover_w, cover_h = 420, 236
    cover_img = _download_cover(s["cover_url"])
    cover = (_cover_round(cover_img, cover_w, cover_h) if cover_img
             else _cover_placeholder(cover_w, cover_h))
    img.paste(cover, (MARGIN, y), cover)

    rx = MARGIN + cover_w + 26
    d.text((rx, y + 2), "直 播 数 据 舱 · 单 场 战 报", font=f_brand, fill=PRIMARY)
    title = s["title"] or "（未获取到直播标题）"
    tl = _wrap(d, title, _font(30, bold=True), W - rx - MARGIN)
    ty = y + 34
    for line in tl:
        d.text((rx, ty), line, font=_font(30, bold=True), fill=TEXT)
        ty += 44
    d.text((rx, ty + 8),
           f"{s['anchor'] or s['vup']}　房间号 {s['room_id'] or '—'}",
           font=_font(18), fill=TEXT2)
    meta = f"{s['date'] or '日期未知'}　直播时长 {_fmt_dur(s['span_sec'])}"
    if s["live_start"]:
        meta += f"　开播 {s['live_start'][11:16]}"
    d.text((rx, ty + 42), meta, font=f_body, fill=TEXT3)
    watched = s.get("watched_show") or s.get("watched_recorded") or ""
    if watched:
        d.text((rx, ty + 74), f"累计看过：{watched}",
               font=f_body, fill=GOLD)
    y += cover_h + 24

    # ---------- 指标宫格 ----------
    cards = [
        ("弹幕总数", _fmt_n(s["danmaku"]), TEXT),
        ("独立观众", _fmt_n(s["users"]), TEXT),
        ("峰值人气", _fmt_n(s["pop_peak"]) if s["has_pop"] else "—", PRIMARY),
        ("平均人气", _fmt_n(s["pop_avg"]) if s["has_pop"] else "—", TEXT),
        ("总营收", f"¥{_fmt_n(s['revenue'])}", GOLD),
        ("SC 条数 / 金额", f"{s['sc_count']} / ¥{_fmt_n(s['sc_amount'])}", GOLD),
        ("金礼物营收", f"¥{_fmt_n(s['gift_amount'])}（{s['gift_count']}件）", TEXT),
        ("弹幕峰值", f"{_fmt_n(s['dm_peak']['count'])} 条/分", BLUE),
        ("互动速率", f"{round(s['danmaku'] / s['span_sec'] * 60, 1) if s['span_sec'] else 0} 条/分", TEXT),
        ("累计看过", s.get("watched_show") or s.get("watched_recorded") or "—", TEXT2),
    ]
    cols, gap = 5, 14
    cw = (content_w - gap * (cols - 1)) // cols
    ch_ = 96
    for i, (label, value, color) in enumerate(cards):
        r, c = divmod(i, cols)
        x = MARGIN + c * (cw + gap)
        cy = y + r * (ch_ + gap)
        _panel(d, img, x, cy, cw, ch_, r=12)
        d.text((x + 14, cy + 14), label, font=f_card_l, fill=TEXT3)
        vf = _font(24, bold=True) if len(str(value)) > 9 else f_card_v
        d.text((x + 14, cy + 42), str(value), font=vf, fill=color)
    y += ch_ * 2 + gap + 24

    # ---------- 人气曲线 ----------
    pop_h = 300
    _panel(d, img, MARGIN, y, content_w, pop_h)
    d.text((MARGIN + 20, y + 16), "观看人气曲线", font=f_h2, fill=TEXT)
    d.text((MARGIN + 20 + _text_w(d, "观看人气曲线", f_h2) + 12, y + 22),
           "25 秒粒度 · B站平台人气值（非真实独立人数）", font=f_small, fill=TEXT3)
    if s["has_pop"]:
        _draw_pop_curve(d, s, MARGIN + 24, y + 64,
                        content_w - 48, pop_h - 96)
    else:
        _draw_empty(d, MARGIN + 24, y + 80, content_w - 48, pop_h - 120,
                    "该文件录制于人气采集上线前，暂无曲线数据（新版录制自动采集）")
    y += pop_h + 16

    # ---------- 弹幕分钟分布 ----------
    dm_h = 210
    _panel(d, img, MARGIN, y, content_w, dm_h)
    d.text((MARGIN + 20, y + 16), "弹幕时间分布", font=f_h2, fill=TEXT)
    if s["dm_bars"]:
        _draw_bars(d, s["dm_bars"], MARGIN + 24, y + 58,
                   content_w - 48, dm_h - 84,
                   t0=s["start_ts"], bucket=s["dm_bucket"])
    else:
        _draw_empty(d, MARGIN + 24, y + 66, content_w - 48, dm_h - 96,
                    "暂无弹幕时间数据")
    y += dm_h + 16

    # ---------- 底部三栏 ----------
    col_w = (content_w - gap * 2) // 3
    list_h = 330
    cols_data = [
        ("高频弹幕 Top10", _render_top_texts(s["top_texts"])),
        ("SC 贡献榜 Top5", _render_top_sc(s["top_sc"])),
        ("粉丝牌 Top5", _render_top_medals(s["top_medals"])),
    ]
    for i, (title_s, rows) in enumerate(cols_data):
        x = MARGIN + i * (col_w + gap)
        _panel(d, img, x, y, col_w, list_h)
        d.text((x + 18, y + 16), title_s, font=f_h2, fill=TEXT)
        ry = y + 58
        for row_color, row_text in rows:
            d.text((x + 18, ry), row_text, font=f_body, fill=row_color)
            ry += 26
            if ry > y + list_h - 30:
                break
    y += list_h + 14

    # ---------- 页脚 ----------
    foot = ("直播数据舱 · 单场战报　|　营收口径：SC + 金礼物（金瓜子÷1000），免费礼物不计　|　"
            f"生成于 {time.strftime('%Y-%m-%d %H:%M', time.localtime())}")
    d.text((MARGIN, y + 8), foot, font=f_small, fill=TEXT3)
    img = img.crop((0, 0, W, y + 44))

    if out_path is None:
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG", optimize=True)
    return out_path.read_bytes()


def _render_top_texts(items):
    rows = []
    for text, cnt in items:
        shown = text if len(text) <= 16 else text[:15] + "…"
        rows.append((TEXT2, f"×{cnt:<4} {shown}"))
    return rows or [(TEXT3, "暂无数据")]


def _render_top_sc(items):
    rows = []
    for u in items:
        name = u.get("uname") or "匿名"
        rows.append((GOLD, f"¥{u['amount']:<8.1f} {name} ×{u['count']}"))
    return rows or [(TEXT3, "本场无 SC")]


def _render_top_medals(items):
    rows = [(TEXT2, f"{name}　{cnt} 条") for name, cnt in items]
    return rows or [(TEXT3, "暂无粉丝牌数据")]


def _draw_empty(d, x, y, w, h, msg):
    d.rounded_rectangle([x, y, x + w, y + h], radius=10, fill=PANEL2)
    f = _font(15)
    d.text((x + w / 2 - d.textlength(msg, font=f) / 2, y + h / 2 - 10),
           msg, font=f, fill=TEXT3)


def _downsample(points, max_n=480):
    if len(points) <= max_n:
        return points
    step = len(points) / max_n
    out = []
    for i in range(max_n):
        seg = points[int(i * step):int((i + 1) * step)]
        if seg:
            ts = seg[len(seg) // 2][0]
            out.append([ts, max(v for _, v in seg)])
    return out


def _draw_pop_curve(d, s, x, y, w, h):
    pts = _downsample(s["pop_series"])
    t0, t1 = pts[0][0], pts[-1][0]
    vmax = max(max(v for _, v in pts), 1)
    span = max(1, t1 - t0)

    # 网格 + Y 轴刻度
    f = _font(12)
    for i in range(4):
        gy = y + h - i * h / 3
        d.line([x, gy, x + w, gy], fill=BORDER, width=1)
        val = int(vmax * i / 3)
        label = f"{val/10000:.1f}万" if val >= 10000 else str(val)
        d.text((x - 6 - d.textlength(label, font=f), gy - 8),
               label, font=f, fill=TEXT3)

    def px(ts):
        return x + (ts - t0) / span * w

    def py(v):
        return y + h - v / vmax * h

    coords = [(px(ts), py(v)) for ts, v in pts]
    # 面积（RGB 模式用深色近似半透明）
    area = [(x, y + h)] + coords + [(x + w, y + h)]
    d.polygon(area, fill=(60, 32, 46))
    # 折线
    d.line(coords, fill=PRIMARY, width=3, joint="curve")
    # 峰值点
    peak_ts, peak_v = max(pts, key=lambda p: p[1])
    pkx, pky = px(peak_ts), py(peak_v)
    d.ellipse([pkx - 6, pky - 6, pkx + 6, pky + 6], fill=RED,
              outline=TEXT, width=2)
    pf = _font(14, bold=True)
    peak_label = f"峰值 {_fmt_n(peak_v)}"
    tw = d.textlength(peak_label, font=pf)
    lx = max(x, min(pkx - tw - 14, x + w - tw - 4))
    ly = max(y + 2, pky - 30)
    d.text((lx, ly), peak_label, font=pf, fill=RED)
    # X 轴时间
    for i in range(5):
        ts = t0 + span * i / 4
        tx = x + w * i / 4
        d.text((tx - 22, y + h + 10),
               time.strftime("%H:%M", time.localtime(ts)),
               font=f, fill=TEXT3)


def _draw_bars(d, bars, x, y, w, h, t0=None, bucket=60):
    vmax = max(max(bars), 1)
    n = len(bars)
    slot = w / n
    bw = max(2, slot * 0.62)
    f = _font(12)
    for i, v in enumerate(bars):
        bh = max(0, v / vmax) * h
        bx = x + i * slot + (slot - bw) / 2
        by = y + h - bh
        d.rectangle([bx, by, bx + bw, y + h], fill=BLUE)
    # 峰值只标第一根达到最大值的柱
    pk = max(range(n), key=lambda i: bars[i])
    bx = x + pk * slot + (slot - bw) / 2
    bh = max(1, bars[pk] / vmax) * h
    d.rectangle([bx, y + h - bh, bx + bw, y + h], fill=RED)
    per_min = bars[pk] * 60 / max(60, bucket)
    peak_txt = f"峰值约 {per_min:.0f} 条/分"
    fpk = _font(13, bold=True)
    tw = d.textlength(peak_txt, font=fpk)
    cx = x + pk * slot + slot / 2
    lx = cx + 10 if cx < x + w / 2 else cx - tw - 10
    lx = max(x, min(lx, x + w - tw))
    d.text((lx, y - 4), peak_txt, font=fpk, fill=RED)
    # X 轴时间
    if t0:
        for i in range(5):
            ts = t0 + bucket * n * i / 4
            tx = x + w * i / 4
            d.text((tx - 22, y + h + 10),
                   time.strftime("%H:%M", time.localtime(ts)),
                   font=f, fill=TEXT3)
