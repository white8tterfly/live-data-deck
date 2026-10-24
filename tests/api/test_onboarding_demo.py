"""新手引导演示数据接口测试：POST / DELETE /api/onboarding/demo。

两个接口只在服务端配置派生的数据目录内造 / 删 __onboarding_demo__ 目录，
不触网；测试断言三类产物（records / weak / video）的建与删。
"""
from __future__ import annotations

import pytest

from recorder.onboarding_demo import DEMO_VUP


@pytest.mark.tc(["TC-OB-I08", "TC-OB-I09"])
def test_onboarding_demo_create_then_delete(client, manager):
    """POST 造出三类演示产物并可被列表接口读出；DELETE 删除后 removed=true，再删为 false。"""
    r = client.post("/api/onboarding/demo")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["vup"] == DEMO_VUP

    rec_dir = manager.settings.records_dir / DEMO_VUP
    weak_dir = manager.settings.weak_dir / DEMO_VUP
    vid_dir = manager.settings.video_path() / DEMO_VUP
    assert rec_dir.is_dir() and weak_dir.is_dir() and vid_dir.is_dir()

    files = client.get("/api/files").json()
    assert any(f["vup"] == DEMO_VUP for f in files["records"])
    assert any(f["vup"] == DEMO_VUP for f in files["weak"])

    r2 = client.delete("/api/onboarding/demo")
    assert r2.status_code == 200 and r2.json()["removed"] is True
    assert not rec_dir.exists() and not weak_dir.exists() and not vid_dir.exists()

    assert client.delete("/api/onboarding/demo").json()["removed"] is False


def test_onboarding_demo_second_call_ok(client, manager):
    """重复 POST 直接重建演示目录，不报错（幂等）。"""
    assert client.post("/api/onboarding/demo").status_code == 200
    r = client.post("/api/onboarding/demo")
    assert r.status_code == 200 and r.json()["ok"] is True

    rec_dir = manager.settings.records_dir / DEMO_VUP
    assert len(list(rec_dir.glob("*.jsonl"))) == 1
