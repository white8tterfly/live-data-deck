"""夹具自检：确认隔离生效、接口可打、且不触发外网。"""
from __future__ import annotations


def test_state_ok(client):
    r = client.get("/api/state")
    assert r.status_code == 200
    body = r.json()
    assert "rooms" in body and "settings" in body


def test_config_isolated(client, isolated_paths):
    r = client.get("/api/logs")
    assert r.status_code == 200
    assert str(isolated_paths["data_dir"]) in r.json()["dir"]
