"""单场直播战报：jsonl 聚合 + Pillow 一页 PNG 长图渲染。

数据口径：
- 人气值（popularity）来自 WS op=3 心跳应答（25s 粒度），是 B 站平台加权
  指标，非真实独立人数；旧版录制文件没有该字段，曲线区显示缺数据提示
- 营收 = SC 金额(price_rmb) + 金礼物（total_coin/1000），银瓜子免费礼物不计
"""
from __future__ import annotations

import io
import json
import math
import time
from collections import Counter
from pathlib import Path

from .filters import record_user_key
from .theme import COLOR_KEYS, ReportTheme

MARGIN = 36
DEFAULT_FONT_REG = "C:/Windows/Fonts/msyh.ttc"
DEFAULT_FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"


def _hex2rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


class Palette:
    """主题色板（RGB tuple），属性名与 theme.COLOR_KEYS 一一对应。"""
    __slots__ = COLOR_KEYS

    def __init__(self, th: ReportTheme):
        for k in COLOR_KEYS:
            setattr(self, k, _hex2rgb(getattr(th, k)))


class FontResolver:
    """按主题加载字体：自定义路径优先，失败回退微软雅黑，再退 PIL 内置默认。"""

    def __init__(self, th: ReportTheme):
        self._scale = th.font_scale
        self._regular = th.font_regular
        self._bold = th.font_bold
        self._cache: dict = {}

    def get(self, size: int, bold: bool = False):
        from PIL import ImageFont
        # 字号档位在加载时整体缩放，缓存按实际像素字号区分
        size = max(1, int(round(size * self._scale)))
        want = self._bold if bold else self._regular
        fallback = DEFAULT_FONT_BOLD if bold else DEFAULT_FONT_REG
        key = (want or fallback, size)
        if key not in self._cache:
            font = None
            for p in (want, fallback):
                if not p:
                    continue
                try:
                    font = ImageFont.truetype(p, size)
                    break
                except OSError:
                    continue
            self._cache[key] = font or ImageFont.load_default()
        return self._cache[key]


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


def _fit_font(draw, txt, fonts: "FontResolver", start_size: int, max_w: int,
              min_size: int = 11, bold: bool = False):
    """从 start_size 起逐号缩小，直到文字宽度不超过 max_w。

    保证任何内容（长数字 / 长用户名）在任何字号档位、任何列数下
    都不会画出卡片/面板边界；返回 (font, actual_size)。
    """
    size = max(1, int(start_size))
    font = fonts.get(size, bold=bold)
    while size > min_size and _text_w(draw, txt, font) > max_w:
        size -= 1
        font = fonts.get(size, bold=bold)
    return font, size


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


def _panel(draw, img, x, y, w, h, pal: Palette, r=16):
    draw.rounded_rectangle([x, y, x + w, y + h], radius=r,
                           fill=pal.panel, outline=pal.border, width=1)


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


def _cover_placeholder(w, h, pal: Palette, fonts: FontResolver):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (w, h), pal.panel2)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=14,
                        outline=pal.border, width=1)
    f = fonts.get(18)
    msg = "暂无封面"
    d.text((w / 2 - _text_w(d, msg, f) / 2, h / 2 - 12), msg,
           font=f, fill=pal.text3)
    return img


# ---------- 指标卡定义：key → (标签, 取值, 色板属性名) ----------

def _card_defs(s: dict):
    rate = round(s["danmaku"] / s["span_sec"] * 60, 1) if s["span_sec"] else 0
    return [
        ("danmaku", "弹幕总数", _fmt_n(s["danmaku"]), "text"),
        ("users", "独立观众", _fmt_n(s["users"]), "text"),
        ("pop_peak", "峰值人气",
         _fmt_n(s["pop_peak"]) if s["has_pop"] else "—", "primary"),
        ("pop_avg", "平均人气",
         _fmt_n(s["pop_avg"]) if s["has_pop"] else "—", "text"),
        ("revenue", "总营收", f"¥{_fmt_n(s['revenue'])}", "gold"),
        ("sc", "SC 条数 / 金额",
         f"{s['sc_count']} / ¥{_fmt_n(s['sc_amount'])}", "gold"),
        ("gift", "金礼物营收",
         f"¥{_fmt_n(s['gift_amount'])}（{s['gift_count']}件）", "text"),
        ("dm_peak", "弹幕峰值",
         f"{_fmt_n(s['dm_peak']['count'])} 条/分", "blue"),
        ("rate", "互动速率", f"{rate} 条/分", "text"),
        ("watched", "累计看过",
         s.get("watched_show") or s.get("watched_recorded") or "—", "text2"),
    ]


# ---------- 五个可排序/可隐藏的主区块 ----------

def _sec_summary(img, d, s, th, pal, fonts, x, y, w):
    # 封面比例约 16:9；横屏宽幅给更大封面，竖屏长图保持紧凑
    if th.orientation == "landscape":
        cover_w, cover_h = 620, 349
    else:
        cover_w, cover_h = 420, 236
    if th.show_cover:
        cover_img = _download_cover(s["cover_url"])
        cover = (_cover_round(cover_img, cover_w, cover_h) if cover_img
                 else _cover_placeholder(cover_w, cover_h, pal, fonts))
        img.paste(cover, (x, y), cover)
        rx = x + cover_w + 26
        text_w = w - cover_w - 26
    else:
        rx, text_w = x, w

    d.text((rx, y + 2), th.brand, font=fonts.get(15), fill=pal.primary)
    title = s["title"] or "（未获取到直播标题）"
    f_title = fonts.get(30, bold=True)
    lines = _wrap(d, title, f_title, text_w)
    line_h = max(1, int(44 * th.font_scale))
    ty = y + 34
    for line in lines:
        d.text((rx, ty), line, font=f_title, fill=pal.text)
        ty += line_h
    d.text((rx, ty + 8),
           f"{s['anchor'] or s['vup']}　房间号 {s['room_id'] or '—'}",
           font=fonts.get(18), fill=pal.text2)
    meta = f"{s['date'] or '日期未知'}　直播时长 {_fmt_dur(s['span_sec'])}"
    if s["live_start"]:
        meta += f"　开播 {s['live_start'][11:16]}"
    d.text((rx, ty + 42), meta, font=fonts.get(16), fill=pal.text3)
    watched = s.get("watched_show") or s.get("watched_recorded") or ""
    if watched:
        d.text((rx, ty + 74), f"累计看过：{watched}",
               font=fonts.get(16), fill=pal.gold)

    if th.show_cover:
        return y + cover_h + 24
    return ty + (74 if watched else 42) + 26 + 24


def _sec_cards(img, d, s, th, pal, fonts, x, y, w):
    active = set(th.active_cards())
    cards = [c for c in _card_defs(s) if c[0] in active]
    if not cards:
        return y  # 卡片全部关闭：不占垂直空间
    cols, gap = th.card_cols, 14
    cw = (w - gap * (cols - 1)) // cols
    ch = max(1, int(96 * th.font_scale))
    pad = 16                       # 卡内左右内边距
    avail = max(1, cw - pad * 2)  # 文字可用宽度
    for i, (_, label, value, cname) in enumerate(cards):
        r, c = divmod(i, cols)
        cx = x + c * (cw + gap)
        cy = y + r * (ch + gap)
        _panel(d, img, cx, cy, cw, ch, pal, r=12)
        # 标签：14px 起步，超长自动缩小，保证单行不出格
        f_label, _ = _fit_font(d, label, fonts, 14, avail, min_size=10)
        d.text((cx + pad, cy + 13), label, font=f_label, fill=pal.text3)
        # 数值：30px 粗体起步（长串从 26px 起），逐号缩放到入格；底对齐
        vtxt = str(value)
        start = 26 if len(vtxt) > 8 else 30
        f_value, vsize = _fit_font(d, vtxt, fonts, start, avail,
                                   min_size=12, bold=True)
        try:
            ascent, descent = f_value.getmetrics()
            vh = ascent + descent
        except AttributeError:
            vh = vsize
        vy = cy + ch - 12 - vh
        d.text((cx + pad, vy), vtxt, font=f_value, fill=getattr(pal, cname))
    rows = (len(cards) + cols - 1) // cols
    return y + rows * ch + (rows - 1) * gap + 24


def _sec_pop(img, d, s, th, pal, fonts, x, y, w):
    pop_h = 400 if th.orientation == "landscape" else 300
    _panel(d, img, x, y, w, pop_h, pal)
    f_h2 = fonts.get(20, bold=True)
    d.text((x + 20, y + 16), "观看人气曲线", font=f_h2, fill=pal.text)
    d.text((x + 20 + _text_w(d, "观看人气曲线", f_h2) + 12, y + 22),
           "25 秒粒度 · B站平台人气值（非真实独立人数）",
           font=fonts.get(13), fill=pal.text3)
    if s["has_pop"]:
        _draw_pop_curve(d, s, x + 24, y + 64, w - 48, pop_h - 96, pal, fonts)
    else:
        _draw_empty(d, x + 24, y + 80, w - 48, pop_h - 120,
                    "该文件录制于人气采集上线前，暂无曲线数据（新版录制自动采集）",
                    pal, fonts)
    return y + pop_h + 16


def _sec_dm(img, d, s, th, pal, fonts, x, y, w):
    dm_h = 280 if th.orientation == "landscape" else 210
    _panel(d, img, x, y, w, dm_h, pal)
    d.text((x + 20, y + 16), "弹幕时间分布",
           font=fonts.get(20, bold=True), fill=pal.text)
    if s["dm_bars"]:
        _draw_bars(d, s["dm_bars"], x + 24, y + 58,
                   w - 48, dm_h - 84, pal, fonts,
                   t0=s["start_ts"], bucket=s["dm_bucket"])
    else:
        _draw_empty(d, x + 24, y + 66, w - 48, dm_h - 96,
                    "暂无弹幕时间数据", pal, fonts)
    return y + dm_h + 16


_RANK_DEFS = {
    "top_texts": ("高频弹幕 Top10", lambda s: _render_top_texts(s["top_texts"])),
    "top_sc": ("SC 贡献榜 Top5", lambda s: _render_top_sc(s["top_sc"])),
    "top_medals": ("粉丝牌 Top5", lambda s: _render_top_medals(s["top_medals"])),
}


def _sec_ranklists(img, d, s, th, pal, fonts, x, y, w):
    keys = th.active_ranks()
    if not keys:
        return y  # 三个榜单全部关闭：不占垂直空间
    gap = 14
    n = len(keys)
    col_w = (w - gap * (n - 1)) // n
    list_h = 380 if th.orientation == "landscape" else 330
    f_h2 = fonts.get(20, bold=True)
    for i, k in enumerate(keys):
        title_s, rows = _RANK_DEFS[k][0], _RANK_DEFS[k][1](s)
        cx = x + i * (col_w + gap)
        _panel(d, img, cx, y, col_w, list_h, pal)
        d.text((cx + 18, y + 16), title_s, font=f_h2, fill=pal.text)
        ry = y + 58
        for cname, row_text in rows:
            # 每行单独 fit：长用户名 / 长弹幕在任何列宽与字号档位下都不出格
            f_body, _ = _fit_font(d, row_text, fonts, 16, col_w - 36,
                                  min_size=11)
            d.text((cx + 18, ry), row_text, font=f_body,
                   fill=getattr(pal, cname))
            ry += 26
            if ry > y + list_h - 30:
                break
    return y + list_h + 14


_SECTION_RENDERERS = {
    "summary": _sec_summary,
    "cards": _sec_cards,
    "pop": _sec_pop,
    "dm": _sec_dm,
    "ranklists": _sec_ranklists,
}


def render_png(s: dict, out_path: Path | None = None,
               theme: ReportTheme | None = None) -> bytes:
    from PIL import Image, ImageDraw

    th = theme or ReportTheme()
    pal = Palette(th)
    fonts = FontResolver(th)
    width = th.width

    H = 4096  # 画布预留，末尾按实际内容裁剪（宽度/字号/区块数均可变）
    img = Image.new("RGB", (width, H), pal.bg)
    d = ImageDraw.Draw(img)
    x = MARGIN
    content_w = width - MARGIN * 2
    y = MARGIN

    # 按主题中的区块顺序流式渲染；show_*=False 的区块跳过但顺序保留
    for sid in th.sections:
        if getattr(th, f"show_{sid}", True):
            y = _SECTION_RENDERERS[sid](
                img, d, s, th, pal, fonts, x, y, content_w)

    if th.show_footer:
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime())
        note = th.footer_note.strip()
        foot = (f"{note}　|　生成于 {ts}" if note else
                "直播数据舱 · 单场战报　|　营收口径：SC + 金礼物"
                "（金瓜子÷1000），免费礼物不计　|　"
                f"生成于 {ts}")
        d.text((MARGIN, y + 8), foot, font=fonts.get(13), fill=pal.text3)
        bottom = y + 44
    else:
        bottom = y + 24
    img = img.crop((0, 0, width, bottom))

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
        rows.append(("text2", f"×{cnt:<4} {shown}"))
    return rows or [("text3", "暂无数据")]


def _render_top_sc(items):
    rows = []
    for u in items:
        name = u.get("uname") or "匿名"
        rows.append(("gold", f"¥{u['amount']:<8.1f} {name} ×{u['count']}"))
    return rows or [("text3", "本场无 SC")]


def _render_top_medals(items):
    rows = [("text2", f"{name}　{cnt} 条") for name, cnt in items]
    return rows or [("text3", "暂无粉丝牌数据")]


def _draw_empty(d, x, y, w, h, msg, pal: Palette, fonts: FontResolver):
    d.rounded_rectangle([x, y, x + w, y + h], radius=10, fill=pal.panel2)
    f = fonts.get(15)
    d.text((x + w / 2 - d.textlength(msg, font=f) / 2, y + h / 2 - 10),
           msg, font=f, fill=pal.text3)


def _fmt_axis(v):
    v = int(round(v))
    return f"{v / 10000:.1f}万" if v >= 10000 else str(v)


def _nice_bounds(vlo, vhi, ticks: int = 4):
    """把数据区间扩成整齐的坐标量程，返回 (lo, hi, step)。"""
    if vhi <= vlo:
        vhi = vlo + max(1.0, abs(vlo) * 0.05)
    raw = (vhi - vlo) / max(1, ticks - 1)
    mag = 10 ** math.floor(math.log10(raw))
    norm = raw / mag
    if norm < 1.5:
        unit = 1
    elif norm < 3:
        unit = 2
    elif norm < 4:
        unit = 2.5
    elif norm < 7:
        unit = 5
    else:
        unit = 10
    step = unit * mag
    lo = math.floor(vlo / step + 1e-9) * step
    hi = math.ceil(vhi / step - 1e-9) * step
    return lo, hi, step


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


def _draw_pop_curve(d, s, x, y, w, h, pal: Palette, fonts: FontResolver):
    pts = _downsample(s["pop_series"])
    t0, t1 = pts[0][0], pts[-1][0]
    span = max(1, t1 - t0)

    # Y 轴量程基于原始采样（下采样只保留段内峰值，会抬高最小值）。
    # 人气全程窄幅波动时，从 0 起轴会把曲线压成贴顶直线，此时截断纵轴
    # 聚焦波动区间，并标注“非从 0 起”避免误读；宽幅（含开播爬坡/掉到 0）
    # 仍保持从 0 起。
    vals = [v for _, v in s["pop_series"]]
    vlo, vhi = min(vals), max(vals)
    if vlo > 0 and (vhi - vlo) / vhi < 0.2:
        pad = max((vhi - vlo) * 0.25, vhi * 0.02)
        ymin, ymax, step = _nice_bounds(max(0, vlo - pad), vhi + pad)
        truncated = ymin > 0
    else:
        ymin, ymax, step = _nice_bounds(0, max(vhi, 1) * 1.03)
        truncated = False

    # 网格 + Y 轴刻度
    f = fonts.get(12)
    ticks = int(round((ymax - ymin) / step))
    for i in range(ticks + 1):
        val = ymin + step * i
        gy = y + h - (val - ymin) / (ymax - ymin) * h
        d.line([x, gy, x + w, gy], fill=pal.border, width=1)
        label = _fmt_axis(val)
        d.text((x - 6 - d.textlength(label, font=f), gy - 8),
               label, font=f, fill=pal.text3)

    if truncated:
        # 纵轴断裂标记（基线左端双折线）+ 右上角文字提示
        zx, zy = x + 12, y + h
        d.line([(zx - 5, zy + 5), (zx + 1, zy + 1),
                (zx - 3, zy - 3), (zx + 3, zy - 7)], fill=pal.text2, width=2)
        note = "纵轴聚焦 · 非从 0 起"
        d.text((x + w - d.textlength(note, font=f), y + 4),
               note, font=f, fill=pal.text3)

    def px(ts):
        return x + (ts - t0) / span * w

    def py(v):
        return y + h - (v - ymin) / (ymax - ymin) * h

    coords = [(px(ts), py(v)) for ts, v in pts]
    # 折线下方填充（面积色由主题提供）
    area = [(x, y + h)] + coords + [(x + w, y + h)]
    d.polygon(area, fill=pal.area_pop)
    # 折线
    d.line(coords, fill=pal.primary, width=3, joint="curve")
    # 峰值点
    peak_ts, peak_v = max(pts, key=lambda p: p[1])
    pkx, pky = px(peak_ts), py(peak_v)
    d.ellipse([pkx - 6, pky - 6, pkx + 6, pky + 6], fill=pal.red,
              outline=pal.text, width=2)
    pf = fonts.get(14, bold=True)
    peak_label = f"峰值 {_fmt_n(peak_v)}"
    tw = d.textlength(peak_label, font=pf)
    lx = max(x, min(pkx - tw - 14, x + w - tw - 4))
    ly = max(y + 2, pky - 30)
    d.text((lx, ly), peak_label, font=pf, fill=pal.red)
    # X 轴时间
    for i in range(5):
        ts = t0 + span * i / 4
        tx = x + w * i / 4
        d.text((tx - 22, y + h + 10),
               time.strftime("%H:%M", time.localtime(ts)),
               font=f, fill=pal.text3)


def _draw_bars(d, bars, x, y, w, h, pal: Palette, fonts: FontResolver,
               t0=None, bucket=60):
    vmax = max(max(bars), 1)
    n = len(bars)
    slot = w / n
    bw = max(2, slot * 0.62)
    f = fonts.get(12)
    for i, v in enumerate(bars):
        bh = max(0, v / vmax) * h
        bx = x + i * slot + (slot - bw) / 2
        by = y + h - bh
        d.rectangle([bx, by, bx + bw, y + h], fill=pal.blue)
    # 峰值只标第一根达到最大值的柱
    pk = max(range(n), key=lambda i: bars[i])
    bx = x + pk * slot + (slot - bw) / 2
    bh = max(1, bars[pk] / vmax) * h
    d.rectangle([bx, y + h - bh, bx + bw, y + h], fill=pal.red)
    per_min = bars[pk] * 60 / max(60, bucket)
    peak_txt = f"峰值约 {per_min:.0f} 条/分"
    fpk = fonts.get(13, bold=True)
    tw = d.textlength(peak_txt, font=fpk)
    cx = x + pk * slot + slot / 2
    lx = cx + 10 if cx < x + w / 2 else cx - tw - 10
    lx = max(x, min(lx, x + w - tw))
    d.text((lx, y - 4), peak_txt, font=fpk, fill=pal.red)
    # X 轴时间
    if t0:
        for i in range(5):
            ts = t0 + bucket * n * i / 4
            tx = x + w * i / 4
            d.text((tx - 22, y + h + 10),
                   time.strftime("%H:%M", time.localtime(ts)),
                   font=f, fill=pal.text3)


def demo_summary() -> dict:
    """主题预览用的内置样例数据（不读文件、不联网，封面走占位图）。"""
    t0 = int(time.time()) - 38 * 60
    n = 92
    pop_series = [
        [t0 + i * 25,
         max(0, int(101000 + 2800 * math.sin(i / 9.0)
                    + 700 * math.sin(i / 2.3) + (i % 5 - 2) * 180))]
        for i in range(n)
    ]
    dm_bars = [
        max(0, int(130 + 95 * math.sin(i / 5.0) + 45 * math.sin(i / 1.7)))
        for i in range(39)
    ]
    dm_bars[22] = 386  # 制造一个弹幕峰值
    return {
        "vup": "示例主播Official",
        "anchor": "示例主播",
        "title": "【示例】这是一条用于预览战报样式的直播标题·可长可短自动换行",
        "room_id": 214525,
        "cover_url": "",
        "live_start": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t0)),
        "date": time.strftime("%Y-%m-%d", time.localtime(t0)),
        "start_ts": t0,
        "end_ts": t0 + 38 * 60,
        "span_sec": 38 * 60,
        "danmaku": 12847,
        "users": 3162,
        "sc_count": 18,
        "sc_amount": 628.0,
        "gift_count": 96,
        "gift_amount": 142.5,
        "revenue": 770.5,
        "pop_series": pop_series,
        "pop_peak": max(v for _, v in pop_series),
        "pop_avg": round(sum(v for _, v in pop_series) / n),
        "has_pop": True,
        "watched_show": "1.2万人看过",
        "watched_recorded": "",
        "dm_peak": {"ts": t0 + 22 * 60, "count": 386},
        "dm_bars": dm_bars,
        "dm_bucket": 60,
        "top_texts": [
            ("哈哈哈哈", 328), ("主播好强", 256), ("6666", 241),
            ("来啦来啦", 198), ("好听！", 176), ("下次什么时候播", 154),
            ("点歌点歌", 133), ("晚安", 121), ("笑死", 109), ("关注了", 96),
        ],
        "top_sc": [
            {"uname": "投喂的猫", "count": 3, "amount": 198.0},
            {"uname": "匿名大佬", "count": 1, "amount": 128.0},
            {"uname": "小**光", "count": 2, "amount": 96.0},
            {"uname": "路人甲", "count": 4, "amount": 60.0},
            {"uname": "鱼**饼", "count": 1, "amount": 30.0},
        ],
        "top_medals": [
            ("猫罐头", 1240), ("粉丝团", 866), ("舰长席", 512),
            ("老粉", 388), ("新手上路", 205),
        ],
    }


def render_demo_png(theme: ReportTheme) -> bytes:
    return render_png(demo_summary(), theme=theme)
