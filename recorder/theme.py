"""战报视觉主题：配色 / 字体 / 排版 / 内容区块开关与顺序。

配置持久化在 config/report_theme.json（与 settings/targets 同目录，原子落盘）。
Web 设置页「战报样式」面板通过 /api/report-theme 读写；命令行/绿色版用户
也可以直接编辑该 JSON 文件（改完重新生成战报即生效，无需重启）。
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import CONFIG_DIR, _atomic_write_json

THEME_FILE = CONFIG_DIR / "report_theme.json"

# ---------- 色板键（12 个实际参与渲染的颜色） ----------
COLOR_KEYS = (
    "bg", "panel", "panel2", "border",
    "text", "text2", "text3",
    "primary", "gold", "blue", "red", "area_pop",
)

DEFAULT_COLORS: dict[str, str] = {
    "bg": "#0D0F15",
    "panel": "#171B26",
    "panel2": "#1D2230",
    "border": "#272E40",
    "text": "#E9ECF3",
    "text2": "#A6AEC2",
    "text3": "#6B7488",
    "primary": "#FB7299",
    "gold": "#F6B93B",
    "blue": "#5B9DFF",
    "red": "#FF5C7C",
    "area_pop": "#3C202E",
}

# 浅色预设（浅底上强调色适当加深以保证对比度）
LIGHT_COLORS: dict[str, str] = {
    "bg": "#F4F6FA",
    "panel": "#FFFFFF",
    "panel2": "#EEF2F8",
    "border": "#D9E0EC",
    "text": "#202633",
    "text2": "#5A6478",
    "text3": "#9099AB",
    "primary": "#E2477E",
    "gold": "#B97A0A",
    "blue": "#2B7CE9",
    "red": "#E5484D",
    "area_pop": "#F6D3DF",
}

# 五个主区块（可排序、可整体隐藏）
SECTION_KEYS = ("summary", "cards", "pop", "dm", "ranklists")

# 底部榜单列（独立显隐；全关时 ranklists 区块自动不渲染）
RANK_KEYS = ("top_texts", "top_sc", "top_medals")

# 指标宫格的 10 张卡片（独立显隐；标签/取值仍由 report.py 定义）
CARD_KEYS = (
    "danmaku", "users", "pop_peak", "pop_avg", "revenue",
    "sc", "gift", "dm_peak", "rate", "watched",
)

# 前端展示用中文元数据（与 report.py 渲染内容保持一致）
SECTION_META = (
    ("summary", "头部信息（封面 / 标题 / 主播 / 时长）"),
    ("cards", "指标宫格"),
    ("pop", "观看人气曲线"),
    ("dm", "弹幕时间分布"),
    ("ranklists", "榜单三栏"),
)
RANK_META = (
    ("top_texts", "高频弹幕 Top10"),
    ("top_sc", "SC 贡献榜 Top5"),
    ("top_medals", "粉丝牌 Top5"),
)
CARD_META = (
    ("danmaku", "弹幕总数"),
    ("users", "独立观众"),
    ("pop_peak", "峰值人气"),
    ("pop_avg", "平均人气"),
    ("revenue", "总营收"),
    ("sc", "SC 条数 / 金额"),
    ("gift", "金礼物营收"),
    ("dm_peak", "弹幕峰值"),
    ("rate", "互动速率"),
    ("watched", "累计看过"),
)

COLOR_META = (
    ("bg", "背景"), ("panel", "卡片/面板"), ("panel2", "次级面板"),
    ("border", "边框"), ("text", "主文字"), ("text2", "次级文字"),
    ("text3", "弱文字/刻度"), ("primary", "主强调色"), ("gold", "营收金色"),
    ("blue", "弹幕柱状"), ("red", "峰值/警示"), ("area_pop", "人气曲线填充"),
)

FONT_EXT = (".ttf", ".ttc", ".otf")
FONT_SCALES = (0.92, 1.0, 1.12)   # 小 / 标准 / 大

# 画布方向：竖屏长图适配手机竖屏浏览（9:16 友好），
# 横屏宽幅适配手机横屏 / 电脑查看（16:9 友好）
ORIENTATIONS = ("portrait", "landscape")
ORIENTATION_META = {
    "portrait": {
        "label": "竖屏长图（手机竖屏看，窄而长）",
        "widths": (1080, 1280, 1440),
        "card_cols": (2, 3, 4),
        "default_width": 1080,
        "default_card_cols": 3,
    },
    "landscape": {
        "label": "横屏宽幅（手机横屏 / 电脑看，宽而短）",
        "widths": (1600, 1920),
        "card_cols": (4, 5, 6),
        "default_width": 1920,
        "default_card_cols": 5,
    },
}
# 校验用并集（允许旧配置里的组合继续加载）
WIDTH_CHOICES = tuple(sorted({
    w for m in ORIENTATION_META.values() for w in m["widths"]}))
CARD_COLS_CHOICES = tuple(sorted({
    c for m in ORIENTATION_META.values() for c in m["card_cols"]}))

_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


@dataclass
class ReportTheme:
    # 配色（#RRGGBB）
    bg: str = DEFAULT_COLORS["bg"]
    panel: str = DEFAULT_COLORS["panel"]
    panel2: str = DEFAULT_COLORS["panel2"]
    border: str = DEFAULT_COLORS["border"]
    text: str = DEFAULT_COLORS["text"]
    text2: str = DEFAULT_COLORS["text2"]
    text3: str = DEFAULT_COLORS["text3"]
    primary: str = DEFAULT_COLORS["primary"]
    gold: str = DEFAULT_COLORS["gold"]
    blue: str = DEFAULT_COLORS["blue"]
    red: str = DEFAULT_COLORS["red"]
    area_pop: str = DEFAULT_COLORS["area_pop"]

    # 字体：空串 = 系统默认（微软雅黑）；字号档位
    font_regular: str = ""
    font_bold: str = ""
    font_scale: float = 1.0

    # 排版
    orientation: str = "portrait"   # portrait 竖屏长图 / landscape 横屏宽幅
    width: int = 1080
    card_cols: int = 3
    brand: str = "直 播 数 据 舱 · 单 场 战 报"
    footer_note: str = ""

    # 区块顺序（始终为 5 个合法 id 的一个排列）；显隐用各 show_* 字段表达，
    # 关闭后顺序仍保留，重新开启时位置不丢
    sections: list[str] = field(
        default_factory=lambda: list(SECTION_KEYS))
    show_summary: bool = True
    show_cards: bool = True
    show_pop: bool = True
    show_dm: bool = True
    show_ranklists: bool = True
    show_cover: bool = True
    show_footer: bool = True
    show_top_texts: bool = True
    show_top_sc: bool = True
    show_top_medals: bool = True

    # 10 张指标卡显隐
    card_danmaku: bool = True
    card_users: bool = True
    card_pop_peak: bool = True
    card_pop_avg: bool = True
    card_revenue: bool = True
    card_sc: bool = True
    card_gift: bool = True
    card_dm_peak: bool = True
    card_rate: bool = True
    card_watched: bool = True

    def active_sections(self) -> list[str]:
        return [sid for sid in self.sections
                if getattr(self, f"show_{sid}", False)]

    def active_cards(self) -> list[str]:
        return [k for k in CARD_KEYS if getattr(self, f"card_{k}", True)]

    def active_ranks(self) -> list[str]:
        return [k for k in RANK_KEYS if getattr(self, f"show_{k}", True)]

    def public_dict(self) -> dict:
        return asdict(self)


def _hex_color(v) -> str:
    s = str(v).strip()
    if not _HEX_RE.match(s):
        raise ValueError(f"颜色值 {s!r} 不合法，需为 #RRGGBB 十六进制格式")
    return s.upper()


def _font_path(v, field_name: str) -> str:
    s = str(v).strip().strip('"')
    if not s:
        return ""
    if not s.lower().endswith(FONT_EXT):
        raise ValueError(f"{field_name} 仅支持 {', '.join(FONT_EXT)} 字体文件")
    p = Path(s).expanduser()
    if not p.is_file():
        raise ValueError(f"{field_name} 不存在：{s}")
    return str(p)


def coerce_theme(patch: dict, base: ReportTheme | None = None) -> ReportTheme:
    """把前端/JSON 来的松散 dict 校验合并为 ReportTheme。

    所有非法值直接抛 ValueError（中文信息），由 API 层回显给用户。
    base=None 时以内置默认深色主题为底。
    """
    if not isinstance(patch, dict):
        raise ValueError("主题配置必须是 JSON 对象")
    base = base or ReportTheme()
    kw = asdict(base)

    for k in COLOR_KEYS:
        if k in patch and patch[k] is not None:
            kw[k] = _hex_color(patch[k])

    for k in ("font_regular", "font_bold"):
        if k in patch:
            kw[k] = _font_path(patch[k], "常规字体" if k == "font_regular" else "粗体字体")

    if patch.get("font_scale") is not None:
        try:
            fs = float(patch["font_scale"])
        except (TypeError, ValueError):
            raise ValueError("字号档位不合法")
        if fs not in FONT_SCALES:
            raise ValueError("字号档位必须是 0.92 / 1.0 / 1.12 之一")
        kw["font_scale"] = fs

    if patch.get("orientation") is not None:
        ori = str(patch["orientation"]).strip()
        if ori not in ORIENTATIONS:
            raise ValueError("画布方向必须是 portrait / landscape 之一")
        kw["orientation"] = ori

    if patch.get("width") is not None:
        try:
            w = int(patch["width"])
        except (TypeError, ValueError):
            raise ValueError("画布宽度必须是整数")
        if w not in WIDTH_CHOICES:
            raise ValueError(f"画布宽度必须是 {WIDTH_CHOICES} 之一")
        kw["width"] = w

    if patch.get("card_cols") is not None:
        try:
            cc = int(patch["card_cols"])
        except (TypeError, ValueError):
            raise ValueError("每行卡片数必须是整数")
        if cc not in CARD_COLS_CHOICES:
            raise ValueError(f"每行卡片数必须是 {CARD_COLS_CHOICES} 之一")
        kw["card_cols"] = cc

    for k, limit in (("brand", 40), ("footer_note", 80)):
        if k in patch and patch[k] is not None:
            s = str(patch[k]).strip()
            if len(s) > limit:
                raise ValueError(f"{k} 最长 {limit} 个字符")
            kw[k] = s

    bool_fields = [
        f"show_{k}" for k in (*SECTION_KEYS, "cover", "footer", *RANK_KEYS)
    ] + [f"card_{k}" for k in CARD_KEYS]
    for k in bool_fields:
        if k in patch:
            kw[k] = bool(patch[k])

    if "sections" in patch and patch["sections"] is not None:
        raw = patch["sections"]
        if not isinstance(raw, list):
            raise ValueError("区块顺序必须是数组")
        ordered, seen = [], set()
        for sid in raw:
            if sid in SECTION_KEYS and sid not in seen:
                ordered.append(sid)
                seen.add(sid)
        # 缺失的按默认顺序补齐，未知/重复项忽略
        ordered += [k for k in SECTION_KEYS if k not in seen]
        kw["sections"] = ordered

    return ReportTheme(**kw)


class ThemeStore:
    def __init__(self) -> None:
        self.theme = self._load()

    def _load(self) -> ReportTheme:
        if not THEME_FILE.exists():
            return ReportTheme()
        try:
            raw = json.loads(THEME_FILE.read_text(encoding="utf-8"))
            return coerce_theme(raw)
        except (json.JSONDecodeError, OSError, ValueError):
            # 配置损坏不应阻断服务/战报生成：回退默认
            return ReportTheme()

    def save(self, theme: ReportTheme) -> None:
        _atomic_write_json(THEME_FILE, theme.public_dict())
        self.theme = theme

    def update(self, patch: dict) -> ReportTheme:
        theme = coerce_theme(patch, self.theme)
        self.save(theme)
        return theme

    def reset(self) -> ReportTheme:
        theme = ReportTheme()
        self.save(theme)
        return theme

    @staticmethod
    def presets() -> dict:
        return {"dark": dict(DEFAULT_COLORS), "light": dict(LIGHT_COLORS)}
