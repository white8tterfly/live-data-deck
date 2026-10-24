"""战报主题与弹幕样式配置接口测试。

覆盖 /api/report-theme（读 / 写 / 复位 / 示例出图）与 /api/danmaku-style
（读 / 写 / 复位）。写接口的非法取值必须 400 且不污染原配置；示例出图返回 PNG 魔数。
"""
from __future__ import annotations

import pytest


# 战报主题

def test_get_report_theme(client):
    """GET /api/report-theme 返回 theme / defaults / presets / meta 四块。"""
    r = client.get("/api/report-theme")
    assert r.status_code == 200
    body = r.json()
    assert {"theme", "defaults", "presets", "meta"} <= set(body)
    assert body["theme"]["bg"].startswith("#")
    assert body["defaults"]["bg"].startswith("#")
    assert body["meta"]["orientations"]


@pytest.mark.tc("TC-RPT-B07")
def test_put_report_theme_valid(client):
    """提交合法主题 patch 返回 200 并回显合并后的主题。"""
    r = client.put("/api/report-theme",
                   json={"bg": "#112233", "show_cover": False})
    assert r.status_code == 200
    th = r.json()["theme"]
    assert th["bg"] == "#112233"
    assert th["show_cover"] is False


def test_put_report_theme_invalid_400(client):
    """非法颜色值 PUT 返回 400，原配置不被污染。"""
    r = client.put("/api/report-theme", json={"bg": "not-a-color"})
    assert r.status_code == 400
    assert client.get("/api/report-theme").json()["theme"]["bg"] == "#0D0F15"


@pytest.mark.tc("TC-RPT-B08")
def test_delete_report_theme_resets(client):
    """DELETE /api/report-theme 把主题恢复默认。"""
    client.put("/api/report-theme", json={"bg": "#112233"})
    r = client.delete("/api/report-theme")
    assert r.status_code == 200
    assert r.json()["theme"]["bg"] == "#0D0F15"


@pytest.mark.tc("TC-RPT-B06")
def test_report_theme_preview_png(client):
    """提交合法主题渲染示例战报，返回 image/png 且带 PNG 魔数。"""
    r = client.post("/api/report-theme/preview", json={"bg": "#112233"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_report_theme_preview_invalid_400(client):
    """非法主题 body（字号档位越界）预览返回 400。"""
    r = client.post("/api/report-theme/preview", json={"font_scale": 9})
    assert r.status_code == 400


# 弹幕样式

def test_get_danmaku_style(client):
    """GET /api/danmaku-style 返回 style 与 meta。"""
    r = client.get("/api/danmaku-style")
    assert r.status_code == 200
    body = r.json()
    assert "style" in body and "meta" in body
    assert body["style"]["speed"] == "normal"
    assert body["meta"]["speeds"]


def test_put_danmaku_style_valid(client):
    """提交合法弹幕样式返回 200 并回显新值。"""
    r = client.put("/api/danmaku-style", json={"speed": "fast", "outline": 3})
    assert r.status_code == 200
    st = r.json()["style"]
    assert st["speed"] == "fast"
    assert st["outline"] == 3


@pytest.mark.tc("TC-DSR-E05")
def test_put_danmaku_style_invalid_400(client):
    """非法枚举值 PUT 返回 400，原配置不被污染。"""
    r = client.put("/api/danmaku-style", json={"speed": "teleport"})
    assert r.status_code == 400
    assert client.get("/api/danmaku-style").json()["style"]["speed"] == "normal"


@pytest.mark.tc("TC-DSR-B02")
def test_delete_danmaku_style_resets(client):
    """DELETE /api/danmaku-style 把样式恢复默认。"""
    client.put("/api/danmaku-style", json={"speed": "fast"})
    r = client.delete("/api/danmaku-style")
    assert r.status_code == 200
    assert r.json()["style"]["speed"] == "normal"
