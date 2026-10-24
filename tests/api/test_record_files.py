"""录制文件 / 分析 / 去重 / 战报接口测试。

样本文件来自 sample_jsonl 夹具（已落在隔离数据目录内）。战报两接口会调
enrich_online 补封面与看过数，本文件统一 patch 成空操作以杜绝外网请求。
"""
from __future__ import annotations

import pytest

from recorder import report as report_mod


def _norm(p: str) -> str:
    """把接口回传的相对路径统一成 / 分隔，规避 Windows 反斜杠差异。"""
    return p.replace("\\", "/")


# 文件列表 / 预览 / 下载

@pytest.mark.tc("TC-FIL-B01")
def test_files_list_records_and_weak(client, sample_jsonl):
    """GET /api/files 返回 records / weak 两个键，样本文件出现在 records 中。"""
    r = client.get("/api/files")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"records", "weak"}
    paths = {_norm(f["path"]) for f in body["records"]}
    assert sample_jsonl["rel"] in paths
    assert body["weak"] == []


@pytest.mark.tc("TC-FIL-B02")
def test_files_preview_ok_and_errors(client, sample_jsonl):
    """预览正常 200；kind 非法 400；路径越界 403；文件不存在 404。"""
    r = client.get("/api/files/preview", params={
        "kind": "records", "path": sample_jsonl["rel"]})
    assert r.status_code == 200
    body = r.json()
    assert body["lines"] and "truncated" in body

    assert client.get("/api/files/preview", params={
        "kind": "bad", "path": "x.jsonl"}).status_code == 400
    assert client.get("/api/files/preview", params={
        "kind": "records", "path": "../../etc/passwd"}).status_code == 403
    assert client.get("/api/files/preview", params={
        "kind": "records", "path": "nope.jsonl"}).status_code == 404


@pytest.mark.tc("TC-FIL-B10")
def test_files_download_ok_and_traversal(client, sample_jsonl):
    """下载正常 200 且 Content-Disposition 带文件名；路径越界 403。"""
    r = client.get("/api/files/download", params={
        "kind": "records", "path": sample_jsonl["rel"]})
    assert r.status_code == 200
    cd = r.headers.get("content-disposition", "")
    assert "live_20260101.jsonl" in cd

    assert client.get("/api/files/download", params={
        "kind": "records", "path": "../../etc/passwd"}).status_code == 403


# 数据分析

@pytest.mark.tc("TC-ANA-B03")
def test_analysis_ok_and_traversal(client, sample_jsonl):
    """POST /api/analysis 正常 200；选中越界路径 403。"""
    r = client.post("/api/analysis", json={
        "paths": [sample_jsonl["rel"]], "kind": "records"})
    assert r.status_code == 200
    assert isinstance(r.json(), dict) and r.json()

    assert client.post("/api/analysis", json={
        "paths": ["../../etc/passwd"]}).status_code == 403


# 后处理去重

@pytest.mark.tc(["TC-FIL-B06", "TC-FIL-B09"])
def test_dedup_preview_and_run(client, sample_jsonl):
    """去重预览与执行正常 200，run 另存 .dedup.jsonl 且原文件保留；对去重产物再跑 400。"""
    rel = sample_jsonl["rel"]
    r = client.post("/api/dedup/preview", json={
        "path": rel, "kind": "records", "window_sec": 300})
    assert r.status_code == 200 and "removed" in r.json()

    r2 = client.post("/api/dedup/run", json={
        "path": rel, "kind": "records", "window_sec": 300})
    assert r2.status_code == 200

    names = {f["name"] for f in client.get("/api/files").json()["records"]}
    assert "live_20260101.dedup.jsonl" in names
    assert sample_jsonl["path"].exists()

    ded_rel = rel.rsplit(".jsonl", 1)[0] + ".dedup.jsonl"
    r3 = client.post("/api/dedup/run", json={"path": ded_rel, "kind": "records"})
    assert r3.status_code == 400


@pytest.mark.tc("TC-ANA-B09")
def test_dedup_run_batch(client, sample_jsonl):
    """批量去重 200：返回逐文件结果，total_removed 等于各文件移除数之和。"""
    r = client.post("/api/dedup/run-batch", json={
        "paths": [sample_jsonl["rel"]], "kind": "records", "window_sec": 300})
    assert r.status_code == 200
    body = r.json()
    assert body["total_removed"] == sum(x["removed"] for x in body["results"])


# 战报

@pytest.mark.tc("TC-RPT-B01")
def test_report_summary_and_image(client, sample_jsonl, monkeypatch):
    """战报摘要 200；战报出图返回 PNG（魔数 \\x89PNG）。"""
    # enrich_online 会去 B 站补封面 / 看过数，测试里换掉，避免外网请求
    monkeypatch.setattr(report_mod, "enrich_online", lambda s, sessdata="": s)
    rel = sample_jsonl["rel"]

    r = client.post("/api/report/summary", json={"path": rel, "kind": "records"})
    assert r.status_code == 200
    assert isinstance(r.json(), dict)

    r2 = client.post("/api/report/image", json={"path": rel, "kind": "records"})
    assert r2.status_code == 200
    assert r2.headers["content-type"].startswith("image/png")
    assert r2.content[:4] == b"\x89PNG"


# 关心名单

@pytest.mark.tc("TC-SET-B12")
def test_cared_users_add_and_errors(client):
    """关心名单：GET 列出；POST 201 新增；非法 key 400；重复 key 无 note 400。"""
    assert client.get("/api/cared-users").status_code == 200

    r = client.post("/api/cared-users", json={"key": "123456", "note": "备注"})
    assert r.status_code == 201
    assert r.json()["key"] == "123456"

    assert client.post("/api/cared-users",
                       json={"key": "not-a-key"}).status_code == 400
    assert client.post("/api/cared-users",
                       json={"key": "123456"}).status_code == 400


@pytest.mark.tc("TC-SET-B13")
def test_cared_users_delete(client):
    """关心名单删除：已存在 200；不存在 404。"""
    client.post("/api/cared-users", json={"key": "123456"})
    assert client.delete("/api/cared-users/123456").status_code == 200
    assert client.delete("/api/cared-users/999999").status_code == 404
