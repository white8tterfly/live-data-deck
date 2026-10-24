"""设置接口测试：PUT /api/settings、凭证检测、扫码登录、LLM 探活。

安全要点：响应体绝不回传 SESSDATA / LLM Key 明文；纯空格 patch 视为「不修改」。
出网点（verify_sessdata / generate_qr / poll_qr / test_connection）均在本文件内
monkeypatch 成替身，测试结束不产生任何外网请求。
"""
from __future__ import annotations

import pytest

import server.app as app_mod
from recorder import bili_login as bili_login_mod
from recorder import llm_analyze as llm_mod


@pytest.mark.tc("TC-SET-B14")
def test_settings_update_ok(client):
    """PUT /api/settings 正常改值返回 200，回显脱敏后的设置。"""
    r = client.put("/api/settings",
                   json={"record_gifts": True, "offline_poll_sec": 30})
    assert r.status_code == 200
    body = r.json()
    assert body["record_gifts"] is True
    assert body["offline_poll_sec"] == 30


def test_settings_invalid_values_400(client):
    """非法取值被拒 400：video_qn 非法档位、records_subdir 带斜杠、整数为负。"""
    assert client.put("/api/settings", json={"video_qn": 123}).status_code == 400
    assert client.put("/api/settings",
                      json={"records_subdir": "a/b"}).status_code == 400
    assert client.put("/api/settings",
                      json={"offline_poll_sec": -1}).status_code == 400


@pytest.mark.tc("TC-SET-I08")
def test_settings_response_hides_secrets(client):
    """凭证不出端：响应体不含 sessdata / llm_api_key 明文，只有 *_set 布尔。"""
    r = client.put("/api/settings", json={
        "sessdata": "SESS-SECRET-123",
        "llm_api_key": "KEY-SECRET-456",
        "llm_base_url": "https://api.example.com/v1",
        "llm_model": "demo-model",
    })
    assert r.status_code == 200
    body = r.json()
    assert "sessdata" not in body and "llm_api_key" not in body
    assert body["sessdata_set"] is True and body["llm_key_set"] is True
    assert "SESS-SECRET-123" not in r.text
    assert "KEY-SECRET-456" not in r.text


@pytest.mark.tc("TC-SET-I03")
def test_settings_whitespace_keeps_secrets(client, manager):
    """纯空格 sessdata / llm_api_key 视为「不修改」，已存值不被清空。"""
    client.put("/api/settings", json={
        "sessdata": "KEEP-SESS", "llm_api_key": "KEEP-KEY",
        "llm_base_url": "https://api.example.com/v1", "llm_model": "demo-model"})

    r = client.put("/api/settings", json={"sessdata": "   ", "llm_api_key": "  "})
    assert r.status_code == 200
    assert manager.settings.sessdata == "KEEP-SESS"
    assert manager.settings.llm_api_key == "KEEP-KEY"
    assert r.json()["sessdata_set"] is True and r.json()["llm_key_set"] is True


@pytest.mark.tc("TC-SET-E01")
def test_settings_guest_mode_ok(client):
    """游客模式：未填 SESSDATA 也能保存设置，sessdata_set 为 false。"""
    r = client.put("/api/settings", json={"record_sc": False})
    assert r.status_code == 200
    assert r.json()["sessdata_set"] is False


# 凭证检测

def test_test_cookie_without_sessdata(client):
    """未填 SESSDATA：test-cookie 返回 200 且 ok=false。"""
    r = client.post("/api/settings/test-cookie", json={})
    assert r.status_code == 200
    assert r.json()["ok"] is False


@pytest.mark.tc("TC-SET-B06")
def test_test_cookie_ok(client, monkeypatch):
    """patch verify_sessdata 后正常路径返回其校验结果。"""
    monkeypatch.setattr(app_mod, "verify_sessdata", lambda sd: {
        "ok": True, "mid": 42, "uname": "测试用户",
        "is_vip": False, "message": "登录有效"})
    r = client.post("/api/settings/test-cookie", json={"sessdata": "X"})
    assert r.status_code == 200
    assert r.json()["ok"] is True and r.json()["mid"] == 42


# 扫码登录

@pytest.mark.tc("TC-SET-B03")
def test_qrcode_generate_and_poll(client, monkeypatch):
    """patch generate_qr / poll_qr 后正常 200；替换成抛错时接口 502。"""
    monkeypatch.setattr(bili_login_mod, "generate_qr", lambda: {
        "qrcode_key": "KEY-1", "qr_image": "data:image/png;base64,AA=="})
    r = client.post("/api/bili-qrcode/generate")
    assert r.status_code == 200
    assert r.json()["qrcode_key"] == "KEY-1"

    monkeypatch.setattr(bili_login_mod, "poll_qr", lambda key: {
        "status": "success", "sessdata": "S"})
    r2 = client.get("/api/bili-qrcode/poll", params={"qrcode_key": "KEY-1"})
    assert r2.status_code == 200
    assert r2.json()["status"] == "success"


def test_qrcode_errors_502(client, monkeypatch):
    """B 站侧异常（抛 BiliLoginError）时，generate / poll 均返回 502。"""
    def boom():
        raise bili_login_mod.BiliLoginError("B 站异常")

    def boom_poll(key):
        raise bili_login_mod.BiliLoginError("B 站异常")

    monkeypatch.setattr(bili_login_mod, "generate_qr", boom)
    monkeypatch.setattr(bili_login_mod, "poll_qr", boom_poll)
    assert client.post("/api/bili-qrcode/generate").status_code == 502
    assert client.get("/api/bili-qrcode/poll",
                      params={"qrcode_key": "KEY-1"}).status_code == 502


# LLM 探活

def test_test_llm_missing_key(client):
    """缺 API Key：test-llm 返回 200 且 ok=false，不发起请求。"""
    r = client.post("/api/settings/test-llm", json={
        "base_url": "https://api.example.com/v1", "model": "demo-model"})
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_test_llm_ok(client, monkeypatch):
    """patch test_connection 成功：正常返回 200 与其结果。"""
    async def fake(*, base_url, api_key, model):
        return {"ok": True, "reply": "正常", "latency_ms": 3}

    monkeypatch.setattr(llm_mod, "test_connection", fake)
    r = client.post("/api/settings/test-llm", json={
        "base_url": "https://api.example.com/v1", "api_key": "k", "model": "demo-model"})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_test_llm_config_error_400(client, monkeypatch):
    """patch test_connection 抛 LLMConfigError → 400。"""
    async def fake(*, base_url, api_key, model):
        raise llm_mod.LLMConfigError("配置不当")

    monkeypatch.setattr(llm_mod, "test_connection", fake)
    r = client.post("/api/settings/test-llm", json={
        "base_url": "https://api.example.com/v1", "api_key": "k", "model": "demo-model"})
    assert r.status_code == 400


def test_test_llm_runtime_error_ok_false(client, monkeypatch):
    """patch test_connection 抛 LLMRuntimeError → 200 且 ok=false。"""
    async def fake(*, base_url, api_key, model):
        raise llm_mod.LLMRuntimeError("连不通")

    monkeypatch.setattr(llm_mod, "test_connection", fake)
    r = client.post("/api/settings/test-llm", json={
        "base_url": "https://api.example.com/v1", "api_key": "k", "model": "demo-model"})
    assert r.status_code == 200
    assert r.json()["ok"] is False
