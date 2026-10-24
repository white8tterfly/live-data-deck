"""recorder 纯函数单元测试（不依赖 client、不出网、无副作用）。

覆盖房间级覆盖项校验、关心用户标识校验、战报文字入格、高能检测的分桶 /
抑制 / 相邻合并 / 三信号标签，以及文本脱敏与匿名化。
"""
from __future__ import annotations

import json

import pytest
from PIL import Image, ImageDraw

from recorder import filters
from recorder import highlights as hl
from recorder import report as rpt
from recorder.config import Target, coerce_room_overrides, validate_cared_key
from recorder.theme import ReportTheme

T0 = 1_704_099_000.0  # 60 的整数倍，弹幕桶起点与场次起点对齐


# 房间级覆盖项

def test_coerce_room_overrides_bool_and_null():
    """布尔覆盖项接受 true / false / null（null=跟随全局）。"""
    assert coerce_room_overrides({}) == {}
    assert coerce_room_overrides({"record_sc": True}) == {"record_sc": True}
    assert coerce_room_overrides({"record_sc": False}) == {"record_sc": False}
    assert coerce_room_overrides({"record_sc": None}) == {"record_sc": None}


def test_coerce_room_overrides_bool_rejects_non_bool():
    """布尔覆盖项收到数字 / 字符串应抛 ValueError。"""
    with pytest.raises(ValueError):
        coerce_room_overrides({"record_sc": 1})
    with pytest.raises(ValueError):
        coerce_room_overrides({"record_sc": "true"})


def test_coerce_room_overrides_video_qn_whitelist():
    """video_qn 白名单内整数或 null 通过，白名单外或布尔值抛 ValueError。"""
    assert coerce_room_overrides({"video_qn": 250}) == {"video_qn": 250}
    assert coerce_room_overrides({"video_qn": None}) == {"video_qn": None}
    with pytest.raises(ValueError):
        coerce_room_overrides({"video_qn": 999})
    with pytest.raises(ValueError):
        coerce_room_overrides({"video_qn": True})


def test_coerce_room_overrides_non_dict_and_setattr():
    """非 dict 抛 ValueError；返回值可直接 setattr 到 Target。"""
    with pytest.raises(ValueError):
        coerce_room_overrides(["x"])
    t = Target(room_id=1, vup="甲")
    for k, v in coerce_room_overrides(
            {"record_sc": True, "video_qn": 400}).items():
        setattr(t, k, v)
    assert t.record_sc is True
    assert t.video_qn == 400


# 关心用户标识

def test_validate_cared_key_digits_and_hex():
    """纯数字 UID 与 16 位十六进制通过（统一小写），其余抛 ValueError。"""
    assert validate_cared_key(" 12345678 ") == "12345678"
    assert validate_cared_key("0123456789ABCDEF") == "0123456789abcdef"
    with pytest.raises(ValueError):
        validate_cared_key("abc")
    with pytest.raises(ValueError):
        validate_cared_key("")
    with pytest.raises(ValueError):
        validate_cared_key("0123456789abcdef0")  # 17 位


# 战报文字入格

@pytest.mark.tc("TC-RPT-I04")
def test_fit_font_shrinks_into_box():
    """超宽文本逐号缩小直到宽度入格；足够短的文本保持起始字号。"""
    fonts = rpt.FontResolver(ReportTheme())
    draw = ImageDraw.Draw(Image.new("RGB", (400, 80)))

    short = "1,234"
    font, size = rpt._fit_font(draw, short, fonts, 20, 500)
    assert size == 20
    assert draw.textlength(short, font=font) <= 500

    long_text = "W" * 12
    w_start = draw.textlength(long_text, font=fonts.get(20))
    w_min = draw.textlength(long_text, font=fonts.get(4))
    if w_min < w_start:  # 字体随字号缩放，才能验证「缩小后入格」
        max_w = w_start - 1
        font2, size2 = rpt._fit_font(draw, long_text, fonts, 20, max_w,
                                     min_size=4)
        assert 4 <= size2 < 20
        assert draw.textlength(long_text, font=font2) <= max_w
    else:  # 极端兜底：位图字体不随字号缩放，只能一路缩到下限
        font2, size2 = rpt._fit_font(draw, long_text, fonts, 20, 1, min_size=4)
        assert size2 == 4


# 高能检测

def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False)
                              for r in rows) + "\n", encoding="utf-8")


def _dm(ts, i):
    return {"_type": "danmaku", "ts": ts, "uid_anon": f"{i:016x}",
            "text": f"弹幕{i}", "uname_anon": "张***"}


def _meta(end_ts=T0 + 1800.0):
    return {"base": "live_20260101_1200", "start_ts": T0, "end_ts": end_ts,
            "segments": [{"file": "seg.flv", "start_ts": T0,
                          "end_ts": end_ts, "discarded": False}]}


def test_detect_highlights_buckets_and_windows(tmp_path):
    """两个相隔 10 分钟的密集桶各成一个窗口，时间轴按 60s 对齐分桶。"""
    jl = tmp_path / "live.jsonl"
    rows = ([_dm(T0 + i, i) for i in range(12)]
            + [_dm(T0 + 600 + j, 100 + j) for j in range(12)])
    _write_jsonl(jl, rows)
    res = hl.detect_highlights(jl, _meta(), 60, 8)
    assert len(res["windows"]) == 2
    assert all(w["danmaku"] == 12 for w in res["windows"])
    ts_list = [t["ts"] for t in res["timeline"]]
    assert all(t % 60 == 0 for t in ts_list)
    assert T0 in ts_list and T0 + 600 in ts_list


@pytest.mark.tc("TC-HL-I02")
def test_detect_highlights_suppresses_near_buckets(tmp_path):
    """相邻桶内的次高能窗口被 120s 互斥抑制，仅保留最强的一个。"""
    jl = tmp_path / "live.jsonl"
    rows = ([_dm(T0 + i, i) for i in range(20)]
            + [_dm(T0 + 60 + j, 100 + j) for j in range(15)])
    _write_jsonl(jl, rows)
    res = hl.detect_highlights(jl, _meta(), 60, 8)
    assert len(res["windows"]) == 1
    assert res["windows"][0]["danmaku"] == 20


@pytest.mark.tc("TC-HL-I02")
def test_detect_highlights_merges_adjacent_picks(tmp_path):
    """间隔恰为抑制边界（3 桶）的两个选中桶合并为一段，统计求和。"""
    jl = tmp_path / "live.jsonl"
    rows = ([_dm(T0 + i, i) for i in range(12)]
            + [_dm(T0 + 180 + k, 100 + k) for k in range(12)])
    _write_jsonl(jl, rows)
    res = hl.detect_highlights(jl, _meta(end_ts=T0 + 600), 60, 8)
    assert len(res["windows"]) == 1
    assert res["windows"][0]["danmaku"] == 24
    assert res["windows"][0]["duration_sec"] == 240


@pytest.mark.tc("TC-HL-I03")
def test_detect_highlights_signal_tags(tmp_path):
    """SC 窗口带「SC连击」标签，人气飙升窗口带「人气飙升」标签。"""
    jl = tmp_path / "live.jsonl"
    rows = [_dm(T0 + i, i) for i in range(12)]
    rows.append({"_type": "super_chat", "ts": T0 + 600, "text": "打钱",
                 "price_rmb": 500, "uname_anon": "李***"})
    rows.append({"_type": "popularity", "ts": T0 + 1200, "popularity": 100})
    rows.append({"_type": "popularity", "ts": T0 + 1210, "popularity": 900})
    rows += [_dm(T0 + 1200 + j, 200 + j) for j in range(12)]
    _write_jsonl(jl, rows)
    res = hl.detect_highlights(jl, _meta(), 60, 8)
    by_ts = {w["start_ts"]: w for w in res["windows"]}
    assert "SC连击" in by_ts[T0 + 600]["tags"]
    assert "人气飙升" in by_ts[T0 + 1200]["tags"]


def test_detect_highlights_empty_note(tmp_path):
    """时间窗内无弹幕 / SC 数据时返回空窗口与中文 note。"""
    jl = tmp_path / "live.jsonl"
    _write_jsonl(jl, [{"_type": "popularity", "ts": T0 + 10, "popularity": 50}])
    res = hl.detect_highlights(jl, _meta(), 60, 8)
    assert res["windows"] == []
    assert res["note"]


def test_detect_highlights_missing_jsonl_raises(tmp_path):
    """jsonl 文件不存在时抛 FileNotFoundError（供 API 层转 404）。"""
    with pytest.raises(FileNotFoundError):
        hl.detect_highlights(tmp_path / "nope.jsonl", _meta(), 60, 8)


def test_detect_highlights_requires_start_ts(tmp_path):
    """meta 缺 start_ts 时抛 ValueError。"""
    jl = tmp_path / "live.jsonl"
    _write_jsonl(jl, [_dm(T0 + i, i) for i in range(12)])
    with pytest.raises(ValueError):
        hl.detect_highlights(jl, {"base": "x", "segments": []}, 60, 8)


# 文本脱敏与匿名化

def test_sanitize_text_replaces_sensitive():
    """链接 / @提及 / QQ / UID / 手机号被替换为占位标记。"""
    assert filters.sanitize_text("看 https://ex.com/a 哦") == "看 【链接】 哦"
    assert filters.sanitize_text("你好@张三 在吗") == "你好@【用户】 在吗"
    assert filters.sanitize_text("qq号1234567") == "【QQ号】"
    assert filters.sanitize_text("uid:123456") == "【UID】"
    assert filters.sanitize_text("电话13812345678") == "电话【手机号】"


def test_is_pure_emoticon():
    """剥除 [表情] 后仅剩空白的才是纯表情弹幕。"""
    assert filters.is_pure_emoticon("[大笑]") is True
    assert filters.is_pure_emoticon("[大笑][微笑]") is True
    assert filters.is_pure_emoticon("你好[大笑]") is False
    assert filters.is_pure_emoticon("") is False


def test_anon_id_stable_and_unique():
    """同一 uid 匿名值稳定；不同 uid 不同；结果为 16 位小写十六进制。"""
    a = filters.anon_id(1001, "salt")
    assert a == filters.anon_id(1001, "salt")
    assert a != filters.anon_id(1002, "salt")
    assert len(a) == 16
    assert all(c in "0123456789abcdef" for c in a)


def test_anon_name_masks():
    """用户名掩码保留首字风格、去识别性。"""
    assert filters.anon_name("张三丰") == "张***"
    assert filters.anon_name("A") == "A***"
    assert filters.anon_name("") == ""


def test_record_user_key_prefers_uid():
    """身份键优先取 uid；uid 缺失退回掩码用户名；两者皆空返回空串。"""
    assert filters.record_user_key({"uid_anon": "abc"}) == "abc"
    assert filters.record_user_key({"uname_anon": "张三"}) == "名:张三"
    assert filters.record_user_key({}) == ""
