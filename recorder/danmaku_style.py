"""弹幕字幕渲染样式：ASS 字幕观感配置（画布 / 字体 / 速度 / 密度 / 颜色 / SC 卡片）。

配置持久化在 config/danmaku_style.json（与 settings/targets 同目录，原子落盘）。
Web 设置页「弹幕样式」面板通过 /api/danmaku-style 读写；场次收尾生成 ASS、
手动「重新生成字幕」「导出弹幕版」均读取本配置；也可以直接编辑该 JSON 文件，
下一次生成字幕即生效，无需重启。

与 recorder/theme.py（战报样式）同构：coerce 校验失败抛 ValueError（中文信息），
配置文件损坏回退默认且不阻断服务。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import CONFIG_DIR, _atomic_write_json

STYLE_FILE = CONFIG_DIR / "danmaku_style.json"

# 画布模式：auto=按视频流真实分辨率（FLV/SPS 解析，ffmpeg 兜底）；manual=强制使用下面宽高
CANVAS_MODES = ("auto", "manual")
CANVAS_MODE_LABELS = {
    "auto": "自动探测视频分辨率（推荐）",
    "manual": "手动指定画布宽高（FLV 解析失败或想要统一尺寸时使用）",
}

# 字号档位（相对基准字号 H/28 的倍率）
FONT_SCALES = (0.85, 1.0, 1.15)
FONT_SCALE_LABELS = {0.85: "小", 1.0: "标准", 1.15: "大"}

# 滚动速度：一条弹幕穿越整块屏幕宽度的基准秒数（越小数越快）
SPEED_CHOICES = ("slow", "normal", "fast")
SPEED_CROSS_SECONDS = {"slow": 11.0, "normal": 9.0, "fast": 7.0}
SPEED_LABELS = {
    "slow": "慢（11 秒过屏，适合长弹幕阅读）",
    "normal": "标准（9 秒，接近网页观感）",
    "fast": "快（7 秒，节奏紧凑）",
}

# 不透明度（1.0 = 完全不透明）
OPACITY_CHOICES = (0.3, 0.5, 0.7, 1.0)
OPACITY_LABELS = {0.3: "30%", 0.5: "50%", 0.7: "70%", 1.0: "100%（不透明）"}

# 弹幕显示区域占屏幕高度比例（从顶部起算）
AREA_CHOICES = (0.25, 0.5, 0.75, 1.0)
AREA_LABELS = {0.25: "1/4 屏（同网页默认，最稀疏）", 0.5: "1/2 屏（推荐）",
               0.75: "3/4 屏", 1.0: "全屏（含底部，可能遮挡画面）"}

# 描边粗细（1080p 基准像素，ScaledBorderAndShadow 随分辨率自动缩放）
OUTLINE_CHOICES = (1, 2, 3, 4)
OUTLINE_LABELS = {1: "细", 2: "标准", 3: "粗", 4: "特粗"}

# SC 兜底等级色（老 jsonl 无 sc_bg 时按金额取色，#RRGGBB；B 站经典等级色近似）
# (下限金额, 底色, 左竖条/强调色)
SC_TIER_COLORS: list[tuple[float, str, str]] = [
    (2000.0, "#8B2028", "#FF5C7C"),
    (1000.0, "#B54A1E", "#FF9B42"),
    (500.0, "#9A7414", "#F6C34B"),
    (100.0, "#1F7A52", "#42D392"),
    (50.0, "#146E78", "#3FC7D9"),
    (0.0, "#2654A8", "#5B9DFF"),
]
SC_FALLBACK_TEXT = "#FFFFFF"
SC_FALLBACK_ACCENT = "#F6C34B"


@dataclass
class DanmakuStyle:
    canvas_mode: str = "auto"
    canvas_w: int = 1920
    canvas_h: int = 1080
    font_family: str = "Microsoft YaHei"
    font_scale: float = 1.0
    speed: str = "normal"
    opacity: float = 1.0
    area_ratio: float = 0.5
    outline: int = 2
    bold: bool = False
    show_color: bool = True       # 彩色弹幕（按发送者颜色）
    fixed_mode: bool = True       # 顶部/底部固定弹幕按原模式渲染
    sc_card: bool = True          # SC 原生风卡片；关闭则退回底部一行文字

    def cross_seconds(self) -> float:
        return SPEED_CROSS_SECONDS.get(self.speed, 9.0)

    def public_dict(self) -> dict:
        return asdict(self)


def _choice_val(v, choices, name):
    """宽松匹配数值/枚举：数字允许 '1' / 1 / 1.0 互转，非法抛 ValueError。"""
    if isinstance(choices, tuple) and choices and isinstance(choices[0], str):
        s = str(v).strip().lower()
        if s not in choices:
            raise ValueError(f"{name} 必须是 {list(choices)} 之一")
        return s
    # 数值枚举
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} 必须是 {list(choices)} 之一")
    for c in choices:
        if abs(f - float(c)) < 1e-9:
            return c
    raise ValueError(f"{name} 必须是 {list(choices)} 之一")


def coerce_style(patch: dict, base: DanmakuStyle | None = None) -> DanmakuStyle:
    """把前端/JSON 来的松散 dict 校验合并为 DanmakuStyle。

    非法值直接抛 ValueError（中文信息），由 API 层回显；base=None 以内置默认值为底。
    """
    if not isinstance(patch, dict):
        raise ValueError("弹幕样式配置必须是 JSON 对象")
    kw = asdict(base or DanmakuStyle())

    if patch.get("canvas_mode") is not None:
        kw["canvas_mode"] = _choice_val(
            patch["canvas_mode"], CANVAS_MODES, "画布模式")

    for k, label in (("canvas_w", "画布宽度"), ("canvas_h", "画布高度")):
        if patch.get(k) is not None:
            try:
                v = int(patch[k])
            except (TypeError, ValueError):
                raise ValueError(f"{label}必须是整数")
            if not 320 <= v <= 7680:
                raise ValueError(f"{label}需在 320–7680 之间")
            kw[k] = v

    if patch.get("font_family") is not None:
        fam = str(patch["font_family"]).strip()
        if not fam or len(fam) > 40:
            raise ValueError("字体名称不能为空，且最长 40 个字符")
        if any(c in fam for c in ("{", "}", ",", "\n", "\r")):
            raise ValueError("字体名称含非法字符")
        kw["font_family"] = fam

    if patch.get("font_scale") is not None:
        kw["font_scale"] = _choice_val(
            patch["font_scale"], FONT_SCALES, "字号档位")
    if patch.get("speed") is not None:
        kw["speed"] = _choice_val(patch["speed"], SPEED_CHOICES, "滚动速度")
    if patch.get("opacity") is not None:
        kw["opacity"] = _choice_val(patch["opacity"], OPACITY_CHOICES, "不透明度")
    if patch.get("area_ratio") is not None:
        kw["area_ratio"] = _choice_val(
            patch["area_ratio"], AREA_CHOICES, "显示区域")
    if patch.get("outline") is not None:
        kw["outline"] = int(_choice_val(
            patch["outline"], OUTLINE_CHOICES, "描边粗细"))

    for k in ("bold", "show_color", "fixed_mode", "sc_card"):
        if k in patch and patch[k] is not None:
            kw[k] = bool(patch[k])

    return DanmakuStyle(**kw)


def style_meta() -> dict:
    """前端渲染下拉/单选用的中文选项元数据。"""
    return {
        "canvas_modes": [{"key": k, "label": CANVAS_MODE_LABELS[k]}
                         for k in CANVAS_MODES],
        "font_scales": [{"key": f, "label": FONT_SCALE_LABELS[f]}
                        for f in FONT_SCALES],
        "speeds": [{"key": k, "label": SPEED_LABELS[k]} for k in SPEED_CHOICES],
        "opacities": [{"key": f, "label": OPACITY_LABELS[f]}
                      for f in OPACITY_CHOICES],
        "areas": [{"key": f, "label": AREA_LABELS[f]} for f in AREA_CHOICES],
        "outlines": [{"key": n, "label": OUTLINE_LABELS[n]}
                     for n in OUTLINE_CHOICES],
    }


class DanmakuStyleStore:
    def __init__(self) -> None:
        self.style = self._load()

    def _load(self) -> DanmakuStyle:
        if not STYLE_FILE.exists():
            return DanmakuStyle()
        try:
            raw = json.loads(STYLE_FILE.read_text(encoding="utf-8"))
            return coerce_style(raw)
        except (json.JSONDecodeError, OSError, ValueError):
            # 配置损坏不阻断录制/服务：回退默认
            return DanmakuStyle()

    def save(self, style: DanmakuStyle) -> None:
        _atomic_write_json(STYLE_FILE, style.public_dict())
        self.style = style

    def update(self, patch: dict) -> DanmakuStyle:
        style = coerce_style(patch, self.style)
        self.save(style)
        return style

    def reset(self) -> DanmakuStyle:
        style = DanmakuStyle()
        self.save(style)
        return style


_STORE: DanmakuStyleStore | None = None


def get_store() -> DanmakuStyleStore:
    """进程内单例：录制协程与 API 共用一份内存配置，页面保存后下一场即生效。"""
    global _STORE
    if _STORE is None:
        _STORE = DanmakuStyleStore()
    return _STORE


def sc_tier_colors(price: float) -> tuple[str, str]:
    """按 SC 金额返回 (底色, 强调色)（#RRGGBB）；老数据无卡片颜色时兜底。"""
    for low, bg, accent in SC_TIER_COLORS:
        if price >= low:
            return bg, accent
    return SC_TIER_COLORS[-1][1], SC_FALLBACK_ACCENT
