"""房间管理与关注导入接口测试：目标 CRUD、录制控制、开播列表分页。

全程不连 B 站：关注列表读取与录制任务均已由夹具或本文件的 monkeypatch 换成替身，
打开目录走 _reveal_in_os 替身，只断言传参。
"""
from __future__ import annotations

import pytest

from recorder import following as fmod


def _raiser(exc: Exception):
    """造一个「调用即抛指定异常」的替身，供 FollowingError 分支用。"""
    def _f(sessdata, page=1):
        raise exc
    return _f


def test_state_returns_snapshot(client):
    """GET /api/state 返回全量运行态快照（rooms / settings / totals 齐备）。"""
    r = client.get("/api/state")
    assert r.status_code == 200
    body = r.json()
    assert set(("rooms", "settings", "totals", "running_count")) <= set(body)


def test_logs_ok_and_limit_out_of_range(client):
    """GET /api/logs 正常返回日志与目录；limit 越界（0 / 1001）触发 422。"""
    r = client.get("/api/logs")
    assert r.status_code == 200
    assert "logs" in r.json() and "dir" in r.json()
    assert client.get("/api/logs", params={"limit": 0}).status_code == 422
    assert client.get("/api/logs", params={"limit": 1001}).status_code == 422
    assert client.get("/api/logs", params={"limit": 300}).status_code == 200


def test_open_log_and_data_folder(client, os_side_effects, isolated_paths):
    """日志 / 数据目录打开：reveal 目标由服务端配置派生，落在隔离数据目录内。"""
    data_dir = isolated_paths["data_dir"].resolve()

    r1 = client.post("/api/logs/open-folder")
    assert r1.status_code == 200
    assert str(os_side_effects["reveal"]).startswith(str(data_dir))

    r2 = client.post("/api/data/open-folder")
    assert r2.status_code == 200
    assert os_side_effects["reveal"] == data_dir


# 目标 CRUD

@pytest.mark.tc("TC-RM-B03")
def test_add_target_ok_and_duplicate(client):
    """新增房间 201 并回显参数；同一房间号重复新增返回 400。"""
    r = client.post("/api/targets",
                    json={"room_id": 1001, "vup": "主播甲"})
    assert r.status_code == 201
    body = r.json()
    assert body["room_id"] == 1001 and body["vup"] == "主播甲"
    assert client.post("/api/targets", json={"room_id": 1001}).status_code == 400


@pytest.mark.tc("TC-RM-E01")
def test_add_target_duplicate_keeps_single(client):
    """重复新增被拒后，房间表里该房间仍只出现一次。"""
    client.post("/api/targets", json={"room_id": 1001})
    client.post("/api/targets", json={"room_id": 1001})
    ids = [t["room_id"] for t in client.get("/api/targets").json()["targets"]]
    assert ids.count(1001) == 1


def test_target_invalid_room_id_rejected(client):
    """room_id 非正数被拒；实际由 pydantic Field(gt=0) 拦截返回 422（见报告）。"""
    assert client.post("/api/targets", json={"room_id": 0}).status_code == 422
    assert client.post("/api/targets", json={"room_id": -5}).status_code == 422


def test_update_target_and_errors(client):
    """PUT 改目录名与房间级覆盖项 200；房间不存在 404；overrides 取值非法 400。"""
    assert client.put("/api/targets/999999", json={"vup": "x"}).status_code == 404

    client.post("/api/targets", json={"room_id": 1002})
    r = client.put("/api/targets/1002", json={
        "vup": "改名后", "enabled": False,
        "overrides": {"record_sc": False, "video_qn": 400}})
    assert r.status_code == 200
    d = r.json()
    assert d["vup"] == "改名后" and d["enabled"] is False
    # PUT 返回的是 Target 扁平字段（GET /api/targets 才把覆盖项收进 overrides）
    assert d["record_sc"] is False
    assert d["video_qn"] == 400

    bad = client.put("/api/targets/1002", json={"overrides": {"video_qn": 123}})
    assert bad.status_code == 400


@pytest.mark.tc(["TC-RM-B10", "TC-RM-E05"])
def test_delete_target_and_repeat_404(client):
    """删除房间 200；对不存在（含已删过）的房间再删返回 404。"""
    client.post("/api/targets", json={"room_id": 1003})
    assert client.delete("/api/targets/1003").status_code == 200
    assert client.delete("/api/targets/1003").status_code == 404


def test_targets_bulk_adds_and_skips(client):
    """批量导入 201：新房间进 added，已存在房间进 skipped。"""
    client.post("/api/targets", json={"room_id": 2001})
    r = client.post("/api/targets/bulk", json={
        "items": [{"room_id": 2001}, {"room_id": 2002, "vup": "新主播"}]})
    assert r.status_code == 201
    body = r.json()
    assert [x["room_id"] for x in body["added"]] == [2002]
    assert body["skipped"] == [2001]


# 关注列表

@pytest.mark.tc("TC-FOL-E01")
def test_followings_without_sessdata_400(client):
    """未配置 SESSDATA：GET /api/followings 返回 400（私有数据需登录）。"""
    r = client.get("/api/followings")
    assert r.status_code == 400
    assert "SESSDATA" in r.json()["detail"]


@pytest.mark.tc("TC-FOL-B12")
def test_followings_bad_page_400(client):
    """已配置凭证时，页码越界（0 / 1001）返回 400。"""
    client.put("/api/settings", json={"sessdata": "FAKE-SESS"})
    assert client.get("/api/followings", params={"page": 0}).status_code == 400
    assert client.get("/api/followings", params={"page": 1001}).status_code == 400


def test_followings_paged_ok(client, monkeypatch):
    """patch fetch_followed_page 后正常分页 200，并标出哪间已在录制目标内。"""
    client.put("/api/settings", json={"sessdata": "FAKE-SESS"})
    client.post("/api/targets", json={"room_id": 5555})

    def fake(sessdata, page=1):
        return {
            "items": [
                {"room_id": 5555, "uid": 1, "uname": "甲", "title": "在播",
                 "live_status": 1, "face": "", "live_time": ""},
                {"room_id": 6666, "uid": 2, "uname": "乙", "title": "未播",
                 "live_status": 0, "face": "", "live_time": ""},
            ],
            "page": page, "page_size": 20, "total": 2,
            "total_page": 1, "live_count": 0,
        }

    monkeypatch.setattr(fmod, "fetch_followed_page", fake)
    r = client.get("/api/followings", params={"page": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["page"] == 1 and body["total"] == 2 and body["has_more"] is False
    items = {i["room_id"]: i for i in body["items"]}
    assert items[5555]["added"] is True
    assert items[6666]["added"] is False


@pytest.mark.tc("TC-FOL-E02")
def test_followings_error_codes(client, monkeypatch):
    """凭证失效 FollowingError(-101) → 401；B 站侧其他错误 → 502。"""
    client.put("/api/settings", json={"sessdata": "FAKE-SESS"})
    monkeypatch.setattr(fmod, "fetch_followed_page",
                        _raiser(fmod.FollowingError("登录态失效", -101)))
    assert client.get("/api/followings").status_code == 401

    monkeypatch.setattr(fmod, "fetch_followed_page",
                        _raiser(fmod.FollowingError("B 站错误", -999)))
    assert client.get("/api/followings").status_code == 502


# 录制控制

@pytest.mark.tc("TC-RM-B08")
def test_room_start_and_stop(client, manager):
    """单房间 start / stop：不存在 404；存在时 200 并切换 running 标记。"""
    assert client.post("/api/rooms/7/start").status_code == 404

    client.post("/api/targets", json={"room_id": 777})
    assert client.post("/api/rooms/777/start").status_code == 200
    assert manager.runtimes[777].running is True
    assert client.post("/api/rooms/777/stop").status_code == 200
    assert manager.runtimes[777].running is False


def test_room_stop_missing_returns_200(client):
    """停止不存在的房间接口未做存在性校验，幂等返回 200（与任务预期 404 不一致，见报告）。"""
    assert client.post("/api/rooms/999999/stop").status_code == 200


def test_room_open_folder(client, manager, os_side_effects):
    """打开房间目录：不存在 404、无 current_file 409、有真实文件 200 且目标合法。"""
    assert client.post("/api/rooms/404404/open-folder").status_code == 404

    client.post("/api/targets", json={"room_id": 8888, "vup": "主播乙"})
    assert client.post("/api/rooms/8888/open-folder").status_code == 409

    rt = manager.runtimes[8888]
    records = manager.settings.records_dir
    f = records / rt.vup / "live_x.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("{}\n", encoding="utf-8")
    rt.current_file = "live_x.jsonl"

    r = client.post("/api/rooms/8888/open-folder")
    assert r.status_code == 200
    target = os_side_effects["reveal"]
    assert target == f.resolve()
    assert records.resolve() in target.parents


def test_start_all_and_stop_all(client):
    """一键启动 / 停止全部房间：均返回 200，start-all 带回实际启动列表。"""
    client.post("/api/targets", json={"room_id": 9001})
    client.post("/api/targets", json={"room_id": 9002, "enabled": False})

    r = client.post("/api/start-all")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and isinstance(body["started"], list)

    assert client.post("/api/stop-all").status_code == 200
